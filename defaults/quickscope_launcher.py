#!/usr/bin/env python3
"""Quickscope launcher.

With no arguments it runs inside the desktop session, started by a systemd
user unit hooked into plasma-core.target (with a KDE autostart entry as a
fallback). It consumes the staged launch, runs the app outside Gamescope and
returns to Gaming Mode when the app exits.

  --prepare    install the unit and apply one-shot session tweaks for the
               staged launch (run by the Decky backend just before switching)
  --restore    undo any one-shot tweaks
  --displays   print the display an app would use and its modes, as JSON
  --moonlight-settings
               print Moonlight's current resolution, frame rate and sync settings
  --recover    after the launcher died mid-session: restore and leave the session
  --diagnostics
               print a shareable report (personal details masked)
  --moonlight-hosts
               print Moonlight's saved hosts and their apps, as JSON
  --start-host-app
               ask the host to start the staged stream's app (after --prepare)
  --quit-host-app
               quit it again when the staged launch is cancelled
  --uninstall  undo tweaks and remove the systemd unit

Every tweak lives in ~/.config or /run and is recorded in undo.json, so
nothing here touches files a SteamOS update replaces.

Standard library only: this runs on the system Python, not Decky's.
"""

import collections
import glob
import http.server
import json
import math
import os
import platform
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

HOME = os.path.expanduser("~")
STATE = os.path.join(HOME, ".local", "state", "quickscope")
PENDING = os.path.join(STATE, "pending.json")
UNDO = os.path.join(STATE, "undo.json")
LOG = os.path.join(STATE, "launcher.log")
KWIN_SCRIPT = os.path.join(STATE, "session.js")
OSD_SCREEN = os.path.join(STATE, "osd.qml")
BACKLIGHT_ROOT = "/sys/class/backlight"
SYSFS_ROOT = "/sys"
# The overlay's request waits at most this long for something to show.
OSD_POLL_TIMEOUT = 300
# How long the overlay stays up after a change, in milliseconds.
OSD_SHOW_TIME = 1500
LOADING_SCREEN = os.path.join(STATE, "loading.qml")
LOADING_STATUS = os.path.join(STATE, "loading.txt")
# Kept apart from undo.json: unlocking needs the desktop session's permissions,
# so a failed unlock waits here for the next session instead of being lost.
WIFI_LOCK = os.path.join(STATE, "wifi_lock.json")
SELF = os.path.abspath(__file__)

AUTOSTART_DIR = os.path.join(HOME, ".config", "autostart")
KSPLASHRC = os.path.join(HOME, ".config", "ksplashrc")
# KDE's power management (idle timers), and its per-power-state profiles.
POWERDEVILRC = os.path.join(HOME, ".config", "powerdevilrc")
POWERDEVIL_PROFILES = ("AC", "Battery", "LowBattery")
KWIN_OUTPUT_CONFIG = os.path.join(HOME, ".config", "kwinoutputconfig.json")
ONESHOT_AUTOSTART = os.path.join(AUTOSTART_DIR, "quickscope-oneshot.desktop")
UNIT_DIR = os.path.join(HOME, ".config", "systemd", "user")
UNIT_NAME = "quickscope-launch.service"
RECOVER_UNIT_NAME = "quickscope-recover.service"
UNIT_TARGET = "plasma-core.target"
PLASMASHELL_UNIT = "plasma-plasmashell.service"
KWIN_SCRIPT_NAME = "quickscope-session"
STEAM_FAST_START_ARGS = ["-silent", "-noverifyfiles", "-skipinitialbootstrap", "-norepairfiles"]
STEAMOS_CLIENT = "/usr/lib/steam/steam"
HYBRID_STEAM_DELAY = 1
# How long to wait for desktop Steam to exit cleanly before returning anyway.
STEAM_SHUTDOWN_TIMEOUT = 8
KEEPER_INTERVAL = 2
WIFI_RECHECK_INTERVAL = 30

# steamosctl (get, set) commands for each power setting.
POWER_SETTINGS = {
    "gpu": ("get-gpu-performance-level", "set-gpu-performance-level"),
    "governor": ("get-cpu-scaling-governor", "set-cpu-scaling-governor"),
    "boost": ("get-cpu-boost-state", "set-cpu-boost-state"),
    "scheduler": ("get-cpu-scheduler", "set-cpu-scheduler"),
    # Power saving makes many Wi-Fi chips add latency spikes to a stream.
    "wifi_powersave": ("get-wifi-power-management-state", "set-wifi-power-management-state"),
}
# Measured streaming over Moonlight (APU power): SteamOS defaults 7.8 W,
# performance 8.5 W, battery saver 4.8 W. CPU boost is most of the difference;
# a TDP limit saved nothing more, the stream already draws under 6 W.
# Both use the kernel's own scheduler: against scx_lavd it streamed with the
# same latency on about 1 W less (3.1 vs 4.2 W) and 40% less CPU.
POWER_PROFILES = {
    "performance": {"gpu": "high", "governor": "performance", "boost": "enabled", "scheduler": "none"},
    "battery": {"gpu": "auto", "governor": "powersave", "boost": "disabled", "scheduler": "none"},
}
# "auto" picks one of these by power source, and follows it while running.
AUTO_PROFILES = {"plugged_in": "performance", "on_battery": "battery"}
POWER_SUPPLY_ROOT = "/sys/class/power_supply"
# Low-battery warnings on the OSD, in percent, checked every BATTERY_POLL
# seconds (about 1% of a Deck's battery while streaming) and shown for
# BATTERY_WARNING_TIME milliseconds.
BATTERY_WARN_LEVELS = (20, 10, 2)
BATTERY_CRITICAL = 2  # at or below: "Battery critical"
BATTERY_POLL = 20
BATTERY_WARNING_TIME = 6000
# Battery status on a tap of "…": shown this long (ms), with the projected
# time from the average of this many BATTERY_POLL samples (5 minutes).
BATTERY_STATUS_TIME = 4000
BATTERY_SAMPLES = 15

# Steam's brightness slider drives the backlight through roughly this curve
# (measured on a Deck LCD: 48% -> 26% of the backlight's range, 100% -> max).
BRIGHTNESS_EXPONENT = 1.84
BRIGHTNESS_STEP = 5
BRIGHTNESS_MIN_PCT = 5
# With the lock off, the starting brightness is still held this long so KDE's
# power management can't override it as the desktop starts.
BRIGHTNESS_SETTLE_TIME = 15

# Brightness shortcut: "…" (Quick Access) + left stick up/down, read from the
# built-in controller's hidraw state reports (left stick Y = bytes 50-51 as
# little-endian int16, up positive). "…" does nothing outside Gaming Mode;
# the Steam button would open desktop Steam's window.
DECK_HID_ID = "000028DE:00001205"
COMBO_BUTTON = (14, 0x04)  # byte, bit mask: the "…" button
COMBO_STICK_THRESHOLD = 20000
COMBO_REPEAT_DELAY = 0.35
COMBO_REPEAT_INTERVAL = 0.12
COMBO_POLL = 0.02
# Emergency exit: hold "…" alone this long to close the app and leave the
# session (the minimal desktop has no other way out of a hung app).
EXIT_HOLD = 3
EXIT_HINT_AFTER = 1
EXIT_REQUESTED = threading.Event()
# After asking the app to close, how long before it's killed.
APP_CLOSE_GRACE = 3

# Quiet session: KDE's own background helpers are skipped for the launch
# session. The user's own autostart apps and services (sync clients keeping
# game saves in sync, etc.) are deliberately left alone.
QUIET_AUTOSTART = {
    "baloo_file.desktop",
    "org.kde.discover.notifier.desktop",
    "org.kde.kdeconnect.daemon.desktop",
    "print-applet.desktop",
    "orca-autostart.desktop",
}
# The KScreen OSD asks how to use a newly seen display, over the app;
# Quickscope sets the layout itself.
QUIET_MASKED_UNITS = ["kde-baloo.service", "plasma-kscreen-osd.service"]

PENDING_MAX_AGE = 300
KWIN_TIMEOUT = 20
# Roughly the last 10-15 launches.
LOG_KEEP_LINES = 400
# Steam may need to start, sign in or update before the game appears.
STEAM_APPEAR_TIMEOUT = 300
POLL_INTERVAL = 2
# Consecutive polls without the game before it's considered closed.
GONE_POLLS = 3

UNIT_TEMPLATE = """\
[Unit]
Description=Quickscope one-shot launch
After=plasma-kwin_x11.service plasma-kwin_wayland.service
PartOf=graphical-session.target
ConditionPathExists={pending}
# If the launcher itself dies, nothing would undo the session or leave it:
# with no panel, there's no way out. Recovery does both.
OnFailure={recover}

[Service]
ExecStart="{python}" "{launcher}"
Slice=app.slice
# Steam may be started from here; don't take it down when the launcher exits.
KillMode=process

[Install]
WantedBy={target}
"""

RECOVER_UNIT_TEMPLATE = """\
[Unit]
Description=Quickscope recovery after the launcher died

[Service]
Type=oneshot
ExecStart="{python}" "{launcher}" --recover
"""

AUTOSTART_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name=Quickscope Launcher
Exec="{python}" "{launcher}"
NoDisplay=true
OnlyShowIn=KDE;
"""

HIDDEN_ENTRY = """\
[Desktop Entry]
Type=Application
Name=Hidden by Quickscope
Hidden=true
"""

# KWin script: when the app's first real window opens, fullscreen and focus it,
# close the loading screen, then disconnect.
SESSION_JS = """\
// Steam on Wayland triggers a screen-share portal dialog at startup; never
// fullscreen that or any other system prompt.
var ignored = ["steam", "steamwebhelper", "plasmashell", "org.kde.plasmashell",
               "krunner", "org.kde.krunner", "ksplashqml", "xwaylandvideobridge",
               "polkit-kde-authentication-agent-1", "org.kde.polkit-kde-authentication-agent-1",
               "org.freedesktop.impl.portal.desktop.kde", "xdg-desktop-portal-kde",
               "org.kde.kwalletd6", "kwalletd6", "org.kde.drkonqi"];
var forceFullscreen = %FORCE_FULLSCREEN%;
var loadingPid = %LOADING_PID%;
// Hybrid: desktop Steam runs only for the controller. The Steam button opens
// its window behind the app, where rendering the store costs 40 percent of a core.
var minimizeSteamWindows = %MINIMIZE_STEAM_WINDOWS%;
// The launcher closes the loading screen itself once the network has settled;
// until then it stays on top of the app.
var holdLoading = %HOLD_LOADING%;
var windowAdded = workspace.windowAdded || workspace.clientAdded;
var windowRemoved = workspace.windowRemoved || workspace.clientRemoved;
var appWindow = null;
function windows() {
    return workspace.windowList ? workspace.windowList() : workspace.clientList();
}
function isLoadingScreen(w) {
    return w.pid === loadingPid || String(w.caption) === "%LOADING_TITLE%";
}
function isOwnOverlay(w) {
    return isLoadingScreen(w) || String(w.caption) === "%OSD_TITLE%";
}
function loadingScreen() {
    var ws = windows();
    for (var i = 0; i < ws.length; i++) {
        if (isLoadingScreen(ws[i])) return ws[i];
    }
    return null;
}
function closeLoadingScreen() {
    var w = loadingScreen();
    if (w) w.closeWindow();
}
function activate(w) {
    if ("activeWindow" in workspace) workspace.activeWindow = w;
    else workspace.activeClient = w;
}
// Whatever should be in front: a held loading screen, otherwise the app. A
// stream's loading screen is an overlay, above everything without focus.
function focusApp() {
    var held = holdLoading ? loadingScreen() : null;
    if (held && held.normalWindow) activate(held);
    else if (appWindow) activate(appWindow);
}
function onWindowAdded(w) {
    if (!w || !w.normalWindow || isOwnOverlay(w)) return;
    if (ignored.indexOf(String(w.resourceClass).toLowerCase()) !== -1) return;
    windowAdded.disconnect(onWindowAdded);
    appWindow = w;
    if (forceFullscreen) w.fullScreen = true;
    focusApp();
    // The app's window is up; the loading screen has done its job.
    if (!holdLoading) closeLoadingScreen();
}
windowAdded.connect(onWindowAdded);
// Moonlight opens a second window for the stream itself: hand focus to the
// app's newest window, not its first.
function isIgnored(w) {
    return isOwnOverlay(w) || ignored.indexOf(String(w.resourceClass).toLowerCase()) !== -1;
}
windowAdded.connect(function (w) {
    if (appWindow && w && w !== appWindow && w.normalWindow && !isIgnored(w) && w.pid === appWindow.pid) {
        appWindow = w;
        focusApp();
    }
});
// A reconnecting stream shows a new loading screen, held like the first.
var holdLoadingScreens = holdLoading;
windowAdded.connect(function (w) {
    if (w && holdLoadingScreens && isLoadingScreen(w)) {
        holdLoading = true;
        if (w.normalWindow) activate(w);
    }
});
windowRemoved.connect(function (w) {
    // Gone, e.g. a stream that dropped: the next app window (Moonlight
    // reconnecting) gets the same treatment as the first.
    if (w === appWindow) {
        appWindow = null;
        windowAdded.connect(onWindowAdded);
    }
    if (holdLoading && isLoadingScreen(w)) {
        holdLoading = false;
        focusApp();
    }
});
function isSteamWindow(w) {
    var cls = String(w && w.resourceClass).toLowerCase();
    return !!w && w.normalWindow && (cls === "steam" || cls === "steamwebhelper");
}
// Minimize, never close: with no tray in this session, closing the main
// window quits Steam (and Steam Input), and closing its startup/update window
// cancels the start. Steam re-shows its window on the Steam button, so keep
// it minimized and hand focus straight back to the app.
function keepMinimized(w) {
    if (!isSteamWindow(w)) return;
    w.minimized = true;
    w.minimizedChanged.connect(function () {
        if (!w.minimized) {
            w.minimized = true;
            focusApp();
        }
    });
}
if (minimizeSteamWindows) {
    windowAdded.connect(keepMinimized);
    var activated = workspace.windowActivated || workspace.clientActivated;
    activated.connect(function (w) {
        if (isSteamWindow(w)) {
            w.minimized = true;
            focusApp();
        }
    });
}
"""

LOADING_TITLE = "Quickscope Loading"
LOADING_QML = """\
import QtQuick
import QtQuick.Window
%(imports)s
Window {
    title: %(title)s
    visible: true
%(placement)s
    color: "black"

    // No pointer over the loading screen.
    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.NoButton
        hoverEnabled: true
        cursorShape: Qt.BlankCursor
    }

    Column {
        anchors.centerIn: parent
        spacing: 24
        // Drawn here rather than a themed BusyIndicator: the same on every
        // Deck whatever the desktop theme, and no Controls import to load.
        Item {
            width: 48
            height: 48
            visible: %(spinner)s
            anchors.horizontalCenter: parent.horizontalCenter
            Canvas {
                anchors.fill: parent
                onPaint: {
                    var ctx = getContext("2d");
                    var c = width / 2, r = c - 3;
                    ctx.reset();
                    ctx.lineWidth = 4;
                    ctx.lineCap = "round";
                    ctx.strokeStyle = "#2a2f38";
                    ctx.beginPath();
                    ctx.arc(c, c, r, 0, 2 * Math.PI);
                    ctx.stroke();
                    ctx.strokeStyle = "#1a9fff";
                    ctx.beginPath();
                    ctx.arc(c, c, r, -Math.PI / 2, Math.PI / 4);
                    ctx.stroke();
                }
            }
            RotationAnimator on rotation {
                from: 0
                to: 360
                duration: 900
                loops: Animation.Infinite
                running: true
            }
        }
        Text {
            id: message
            text: %(message)s
            color: "#d0d0d0"
            font.pixelSize: 26
            anchors.horizontalCenter: parent.horizontalCenter
        }
        // The step under way, e.g. Moonlight's "Starting RTSP handshake…".
        Text {
            id: detail
            text: ""
            visible: text.length > 0
            color: "#8a8f98"
            font.pixelSize: 18
            anchors.horizontalCenter: parent.horizontalCenter
        }
        // The still version: an underline instead of the spinner, for a screen
        // that may stay frozen on display through the switch back.
        Rectangle {
            visible: !%(spinner)s
            width: 64
            height: 4
            radius: 2
            color: "#1a9fff"
            anchors.horizontalCenter: parent.horizontalCenter
        }
    }

    // The launcher writes a new message here while it waits (e.g. for Wi-Fi),
    // optionally with the step under way on a second line.
    Timer {
        interval: 200
        running: true
        repeat: true
        onTriggered: {
            var xhr = new XMLHttpRequest();
            xhr.open("GET", %(status_url)s, false);
            try {
                xhr.send();
                if (xhr.responseText.length > 0) {
                    var lines = xhr.responseText.split("\\n");
                    message.text = lines[0];
                    detail.text = lines.length > 1 ? lines[1] : "";
                }
            } catch (e) {}
        }
    }
}
"""
LOADING_WINDOW = "    visibility: Window.FullScreen"
# Streams: an overlay like the OSD's, so no window can get in front of it.
# Moonlight's own windows take focus while it connects, and a normal window
# only stays in front for as long as KWin keeps it focused.
LOADING_OVERLAY_IMPORTS = "import org.kde.layershell 1.0 as LayerShell"
LOADING_OVERLAY = """\
    LayerShell.Window.layer: LayerShell.Window.LayerOverlay
    LayerShell.Window.anchors: LayerShell.Window.AnchorTop | LayerShell.Window.AnchorBottom
                               | LayerShell.Window.AnchorLeft | LayerShell.Window.AnchorRight
    LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityNone
    LayerShell.Window.exclusionZone: -1
    LayerShell.Window.scope: "quickscope-loading\""""
RETURNING_TITLE = "Quickscope Returning"
# The returning screen, ready during the session: Moonlight closes its
# windows within 0.1 s of a quit, faster than a new window (or a hidden one
# shown for the first time) gets its first frame on screen. So it's drawn
# from the start in the background layer, under the fullscreen app where
# nothing sees it (and the app can still go straight to the display), and
# raised to the overlay layer when it's time. Like the OSD, it waits on a
# long poll rather than polling. It looks like the loading screen's still
# version.
RETURNING_QML = """\
import QtQuick
import QtQuick.Window
import org.kde.layershell 1.0 as LayerShell

Window {
    id: screen
    title: %(title)s
    visible: true
    color: "black"
    property bool shown: false
    LayerShell.Window.layer: shown ? LayerShell.Window.LayerOverlay : LayerShell.Window.LayerBackground
    LayerShell.Window.anchors: LayerShell.Window.AnchorTop | LayerShell.Window.AnchorBottom
                               | LayerShell.Window.AnchorLeft | LayerShell.Window.AnchorRight
    LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityNone
    LayerShell.Window.exclusionZone: -1
    LayerShell.Window.scope: "quickscope-returning"

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.NoButton
        hoverEnabled: true
        cursorShape: Qt.BlankCursor
    }

    Column {
        anchors.centerIn: parent
        spacing: 24
        Text {
            text: %(message)s
            color: "#d0d0d0"
            font.pixelSize: 26
            anchors.horizontalCenter: parent.horizontalCenter
        }
        Rectangle {
            width: 64
            height: 4
            radius: 2
            color: "#1a9fff"
            // A change to draw: the new layer only applies with the next
            // frame, and an unchanged window draws none.
            opacity: screen.shown ? 1 : 0.99
            anchors.horizontalCenter: parent.horizontalCenter
        }
    }

    Timer { id: retry; interval: 1000; onTriggered: screen.poll() }

    function poll() {
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE) return;
            if (xhr.responseText === "show") screen.shown = true;
            else if (xhr.status === 200) screen.poll();
            else retry.start();
        };
        xhr.open("GET", %(url)s);
        xhr.send();
    }

    // Tells the launcher, for its log.
    function report(what) {
        var xhr = new XMLHttpRequest();
        xhr.open("GET", %(url)s + what);
        xhr.send();
    }

    property bool readyReported: false
    property bool raisedReported: false
    onFrameSwapped: {
        if (!readyReported) { readyReported = true; report("ready"); }
        if (shown && !raisedReported) { raisedReported = true; report("raised"); }
    }
    onShownChanged: requestUpdate()

    Component.onCompleted: poll()
}
"""
# Seconds into the session before the hidden returning screen starts, clear
# of the app's own start.
RETURNING_PREPARE_DELAY = 5
OSD_TITLE = "Quickscope OSD"
# On-screen indicator for volume and brightness. Drawn as a layer-shell overlay
# (QT_WAYLAND_SHELL_INTEGRATION=layer-shell) so it shows above fullscreen apps
# without taking focus. It asks the launcher's local server what to show, and
# the server answers only once there's something new (a long poll): polling a
# file 10 times a second woke it all session for about 0.5% of a core.
OSD_QML = """\
import QtQuick
import org.kde.layershell 1.0 as LayerShell

Window {
    id: osd
    title: %(title)s
    // Room for the longest text (the battery's); the box itself fits its row.
    width: 640
    height: 120
    visible: false
    color: "transparent"
    LayerShell.Window.layer: LayerShell.Window.LayerOverlay
    LayerShell.Window.anchors: LayerShell.Window.AnchorBottom
    LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityNone
    LayerShell.Window.exclusionZone: -1
    LayerShell.Window.scope: "quickscope-osd"

    property int seq: 0
    property string label: ""
    property real level: 0
    property string valueText: ""

    Rectangle {
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 48
        width: Math.min(parent.width, Math.max(420, row.implicitWidth + 48))
        height: 56
        radius: 10
        color: "#e6171a21"

        Row {
            id: row
            anchors.centerIn: parent
            spacing: 16
            Text {
                text: osd.label
                width: Math.max(104, implicitWidth)
                color: "#dcdedf"
                font.pixelSize: 20
                anchors.verticalCenter: parent.verticalCenter
            }
            Rectangle {
                width: 160
                height: 8
                radius: 4
                color: "#3d4450"
                anchors.verticalCenter: parent.verticalCenter
                Rectangle {
                    width: parent.width * Math.max(0, Math.min(osd.level, 1))
                    height: parent.height
                    radius: 4
                    color: "#1a9fff"
                }
            }
            Text {
                text: osd.valueText
                color: "#ffffff"
                font.pixelSize: 20
                width: Math.max(64, implicitWidth)
                anchors.verticalCenter: parent.verticalCenter
            }
        }
    }

    Timer { id: hide; interval: 1500; onTriggered: osd.visible = false }
    Timer { id: retry; interval: 1000; onTriggered: osd.poll() }

    function poll() {
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE) return;
            try {
                var s = JSON.parse(xhr.responseText);
            } catch (e) {
                retry.start();
                return;
            }
            if (s.seq !== osd.seq) {
                osd.seq = s.seq;
                osd.label = s.label;
                osd.level = s.level;
                osd.valueText = s.text;
                osd.visible = true;
                hide.interval = s.duration;
                hide.restart();
            }
            osd.poll();
        };
        xhr.open("GET", %(state_url)s + "?seq=" + osd.seq);
        xhr.send();
    }

    Component.onCompleted: poll()
}
"""
# Without the KWin script: keep the loading screen up briefly after a Steam
# game starts so its window is on top first, and for direct launches give up
# after a fixed time.
LOADING_LINGER = 3
LOADING_FALLBACK = 6


# --- helpers -----------------------------------------------------------------


def log(msg):
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {msg}\n")
    except OSError:
        pass


def trim_log(keep=LOG_KEEP_LINES):
    """Keep only the most recent lines of the log (it's appended on every launch)."""
    try:
        with open(LOG) as f:
            lines = f.readlines()
        if len(lines) > keep:
            with open(LOG, "w") as f:
                f.writelines(lines[-keep:])
    except OSError:
        pass


def remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log(f"unreadable {path}: {e}")
        return None


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def run(cmd):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as e:
        log(f"{cmd[0]}: {e}")
        return None


def succeeded(result):
    return result is not None and result.returncode == 0


def systemctl(*args):
    return run(["systemctl", "--user", *args])


def dbus(dest, path, method, *args):
    result = run(
        ["dbus-send", "--session", "--print-reply", f"--dest={dest}", "--type=method_call", path, method, *args]
    )
    return result.stdout if succeeded(result) else None


def python_exe():
    return "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable


class SessionKeeper(threading.Thread):
    """Re-applies session settings that something else resets while the app
    runs: desktop Steam puts the GPU performance level back to auto when it
    starts, and KDE's power management changes the brightness."""

    def __init__(self, power, brightness_raw, hold_brightness_for=None, auto_profile=None):
        super().__init__(daemon=True)
        self.power = power
        # With the automatic profile, the profile currently in use.
        self.auto_profile = auto_profile
        self.brightness_raw = brightness_raw
        # Without the lock, only hold brightness while KDE starts up.
        self.release_at = None if hold_brightness_for is None else time.monotonic() + hold_brightness_for
        self.wifi_links = wifi_link_changes()
        self.next_wifi_check = time.monotonic() + WIFI_RECHECK_INTERVAL
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.wait(KEEPER_INTERVAL):
            now = time.monotonic()
            if self.release_at is not None and now >= self.release_at:
                self.brightness_raw = self.release_at = None
            # Wi-Fi power saving needs steamosctl to read, so only look after
            # the link went down or up, plus a slow check in case one is missed.
            links = wifi_link_changes()
            check_wifi = links != self.wifi_links or now >= self.next_wifi_check
            if check_wifi:
                self.wifi_links, self.next_wifi_check = links, now + WIFI_RECHECK_INTERVAL
            switched = False
            if self.auto_profile:
                wanted = resolve_power_profile("auto")
                if wanted != self.auto_profile:
                    self.auto_profile = wanted
                    # Session extras such as Wi-Fi power saving carry over.
                    self.power = {**self.power, **POWER_PROFILES[wanted]}
                    switched = True
            changed = {}
            for key, value in self.power.items():
                if key == "wifi_powersave" and not check_wifi:
                    continue
                current = get_power_setting(key)
                if current is not None and current != value and set_power_setting(key, value):
                    changed[key] = current
            if switched:
                log(f"power source changed, switched to the {self.auto_profile} profile (was {changed})")
            elif changed:
                log(f"re-applied {self.power} after something reset {changed}")
            if self.brightness_raw is not None:
                current = read_backlight()
                if current and current[0] != self.brightness_raw and write_backlight(self.brightness_raw):
                    log(f"brightness was changed to {current[0]}, locked back to {self.brightness_raw}")

    def stop(self):
        # Wait so a check in flight can't re-apply after restore() resets things.
        self.stopped.set()
        self.join(timeout=5)


def wifi_link_changes():
    """How often the wireless links have gone down or up (the kernel's
    carrier_changes): a reconnect, or NetworkManager restarting, can turn
    the driver's power saving back on."""
    total = 0
    for wireless in glob.glob(os.path.join(SYSFS_ROOT, "class/net/*/wireless")):
        value = read_text(os.path.join(os.path.dirname(wireless), "carrier_changes"))
        total += int(value) if value and value.isdigit() else 0
    return total


def backlight_dir():
    dirs = sorted(glob.glob(os.path.join(BACKLIGHT_ROOT, "*")))
    return dirs[0] if dirs else None


def read_backlight():
    """(brightness, max_brightness) of the first backlight, or None."""
    d = backlight_dir()
    if not d:
        return None
    try:
        with open(os.path.join(d, "brightness")) as f, open(os.path.join(d, "max_brightness")) as m:
            return int(f.read().strip()), int(m.read().strip())
    except (OSError, ValueError):
        return None


def write_backlight(raw):
    d = backlight_dir()
    if not d:
        return False
    try:
        with open(os.path.join(d, "brightness"), "w") as f:
            f.write(str(int(raw)))
        return True
    except OSError as e:
        log(f"could not set brightness: {e}")
        return False


def brightness_pct_to_raw(pct, max_raw):
    """Map a Steam-style brightness percentage to a raw backlight value."""
    pct = max(1, min(100, pct))
    return max(1, round(max_raw * (pct / 100) ** BRIGHTNESS_EXPONENT))


def parse_volume(volume_out, mute_out):
    """(percent, muted) from `pactl get-sink-volume` / `get-sink-mute` output."""
    m = re.search(r"(\d+)%", volume_out or "")
    if not m:
        return None
    return int(m.group(1)), "yes" in (mute_out or "").lower()


def read_volume():
    vol = run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
    mute = run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])
    if not (succeeded(vol) and succeeded(mute)):
        return None
    return parse_volume(vol.stdout, mute.stdout)


def brightness_raw_to_pct(raw, max_raw):
    """Inverse of brightness_pct_to_raw, rounded to a whole percent."""
    if max_raw <= 0:
        return 0
    return round(100 * (max(0, raw) / max_raw) ** (1 / BRIGHTNESS_EXPONENT))


def step_brightness_pct(pct, direction):
    """Next brightness on the BRIGHTNESS_STEP grid, clamped like the Steam slider."""
    if direction > 0:
        pct = (pct // BRIGHTNESS_STEP + 1) * BRIGHTNESS_STEP
    else:
        pct = (-(-pct // BRIGHTNESS_STEP) - 1) * BRIGHTNESS_STEP
    return max(BRIGHTNESS_MIN_PCT, min(100, pct))


class Osd:
    """The on-screen indicator overlay. Without plasmashell nothing else draws
    volume or brightness changes."""

    def __init__(self):
        self.proc = None
        self.server = None
        self.state = {"seq": 0, "label": "", "level": 0, "text": "", "duration": OSD_SHOW_TIME}
        self.changed = threading.Condition()

    def start(self):
        qml = shutil.which("qml6") or shutil.which("qml")
        if not qml:
            log("no qml runtime, skipping the on-screen indicator")
            return False
        try:
            self.server = self.serve()
        except OSError as e:
            log(f"on-screen indicator server failed to start: {e}")
            return False
        url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        write_file(OSD_SCREEN, OSD_QML % {"title": json.dumps(OSD_TITLE), "state_url": json.dumps(url)})
        env = dict(os.environ, QT_WAYLAND_SHELL_INTEGRATION="layer-shell")
        try:
            self.proc = subprocess.Popen(
                [qml, OSD_SCREEN], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError as e:
            log(f"on-screen indicator failed to start: {e}")
            self.server.shutdown()
            return False
        return True

    def serve(self):
        osd = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen = re.search(r"[?&]seq=(\d+)", self.path)
                seen = int(seen.group(1)) if seen else -1
                with osd.changed:
                    # Answer at once if the overlay is behind, else when something
                    # changes; the timeout only stops a request living forever.
                    osd.changed.wait_for(lambda: osd.state["seq"] != seen, timeout=OSD_POLL_TIMEOUT)
                    body = json.dumps(osd.state).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def show(self, label, level, text, duration=OSD_SHOW_TIME):
        with self.changed:
            self.state = {"seq": self.state["seq"] + 1, "label": label, "level": level, "text": text}
            self.state["duration"] = duration
            self.changed.notify_all()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class VolumeWatcher:
    """Shows the volume on the OSD whenever it changes."""

    def __init__(self, osd):
        self.osd = osd
        self.watch = None
        self.last = None

    def start(self):
        if not shutil.which("pactl"):
            log("no pactl, skipping the volume indicator")
            return False
        self.last = read_volume()
        try:
            self.watch = subprocess.Popen(
                ["pactl", "subscribe"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
            )
        except OSError as e:
            log(f"volume indicator failed to start: {e}")
            return False
        threading.Thread(target=self._follow, daemon=True).start()
        log("volume indicator running")
        return True

    def _follow(self):
        for line in self.watch.stdout:
            # One volume change produces several events; only show real changes.
            if "on sink" not in line and "on server" not in line:
                continue
            current = read_volume()
            if current and current != self.last:
                self.last = current
                volume, muted = current
                self.osd.show("Volume", 0 if muted else min(volume, 100) / 100, "Muted" if muted else f"{volume}%")

    def stop(self):
        if self.watch and self.watch.poll() is None:
            self.watch.terminate()


def deck_controller_node():
    """The hidraw node that streams the built-in controller's state reports."""
    for sysdir in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(sysdir, "device", "uevent")) as f:
                if DECK_HID_ID not in f.read():
                    continue
        except OSError:
            continue
        path = "/dev/" + os.path.basename(sysdir)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            end = time.monotonic() + 0.3
            while time.monotonic() < end:
                if not select.select([fd], [], [], 0.1)[0]:
                    continue
                try:
                    report = os.read(fd, 128)
                except BlockingIOError:
                    continue
                if is_deck_state_report(report):
                    return path
        finally:
            os.close(fd)
    return None


def is_deck_state_report(report):
    return len(report) == 64 and report[0] == 1 and report[1] == 0 and report[2] == 9


def combo_button_held(report):
    byte, mask = COMBO_BUTTON
    return bool(report[byte] & mask)


def combo_direction(report):
    """+1/-1 while "…" is held with the left stick pushed up/down, else 0."""
    byte, mask = COMBO_BUTTON
    if not report[byte] & mask:
        return 0
    y = int.from_bytes(report[50:52], "little", signed=True)
    if y >= COMBO_STICK_THRESHOLD:
        return 1
    if y <= -COMBO_STICK_THRESHOLD:
        return -1
    return 0


class BrightnessCombo(threading.Thread):
    """ "…" + left stick up/down changes the brightness, like hardware keys;
    a tap calls `on_tap` (the battery), and a 3 s hold quits. Reads the
    controller passively, so Steam and the app still see everything."""

    def __init__(self, osd, keeper, on_tap=None):
        super().__init__(daemon=True)
        self.osd = osd
        self.keeper = keeper
        self.on_tap = on_tap
        self.stopped = threading.Event()
        self.held, self.next_step = 0, 0.0
        # Holding "…" alone is the way out: a stuck or hung app, or a session
        # with no other exit. Moving the stick during the press (brightness)
        # cancels it.
        self.pressed_since, self.used_for_brightness, self.shown = None, False, -1

    def run(self):
        node = deck_controller_node()
        if not node:
            log("no Deck controller found, brightness shortcut off")
            return
        try:
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            log(f"brightness shortcut can't read {node}: {e}")
            return
        log(f"brightness shortcut reading {node}")
        try:
            # The controller reports ~250 times a second. Waking for each costs
            # about 1% of a core all session, so check every COMBO_POLL and
            # look only at the newest report.
            while not self.stopped.wait(COMBO_POLL):
                report = None
                try:
                    while True:
                        chunk = os.read(fd, 128)
                        if is_deck_state_report(chunk):
                            report = chunk
                except BlockingIOError:
                    pass
                except OSError as e:
                    log(f"brightness shortcut stopped: {e}")
                    return
                if report is not None:
                    self.handle(report, time.monotonic())
        finally:
            os.close(fd)

    def handle(self, report, now):
        direction = combo_direction(report)
        if not combo_button_held(report):
            # A quick tap, before the quit hint: the battery, as Gaming Mode's
            # Quick Access menu would show it.
            tapped = self.pressed_since is not None and now - self.pressed_since < EXIT_HINT_AFTER
            if tapped and not self.used_for_brightness and self.on_tap:
                self.on_tap()
            self.pressed_since, self.used_for_brightness, self.shown = None, False, -1
        else:
            self.pressed_since = self.pressed_since or now
            self.used_for_brightness = self.used_for_brightness or bool(direction)
            held_for = now - self.pressed_since
            if not self.used_for_brightness and held_for >= EXIT_HINT_AFTER and not EXIT_REQUESTED.is_set():
                progress = min(1.0, (held_for - EXIT_HINT_AFTER) / (EXIT_HOLD - EXIT_HINT_AFTER))
                if self.osd and int(progress * 10) != self.shown:
                    self.shown = int(progress * 10)
                    self.osd.show("Hold to quit", progress, "")
                if held_for >= EXIT_HOLD:
                    log('"…" held: closing the app')
                    EXIT_REQUESTED.set()
        if direction != self.held:
            self.held = direction
            if direction:
                self.step(direction)
                self.next_step = now + COMBO_REPEAT_DELAY
        elif direction and now >= self.next_step:
            self.step(direction)
            self.next_step = now + COMBO_REPEAT_INTERVAL

    def step(self, direction):
        current = read_backlight()
        if not current:
            return
        raw, max_raw = current
        pct = step_brightness_pct(brightness_raw_to_pct(raw, max_raw), direction)
        target = brightness_pct_to_raw(pct, max_raw)
        if self.keeper and self.keeper.brightness_raw is not None:
            # Set first, so the keeper doesn't put the old value back.
            self.keeper.brightness_raw = target
        write_backlight(target)
        if self.osd:
            self.osd.show("Brightness", pct / 100, f"{pct}%")

    def stop(self):
        # Wait so a step in flight can't land after restore().
        self.stopped.set()
        self.join(timeout=1)


def read_power_sysfs(key):
    """The kernel's own copy of a power setting, in steamosctl's terms, or None.
    The keeper checks every couple of seconds; a file read is far cheaper than
    starting steamosctl three times."""
    if key == "gpu":
        paths = glob.glob(os.path.join(SYSFS_ROOT, "class/drm/card*/device/power_dpm_force_performance_level"))
        return read_text(paths[0]) if paths else None
    if key == "governor":
        return read_text(os.path.join(SYSFS_ROOT, "devices/system/cpu/cpu0/cpufreq/scaling_governor"))
    if key == "boost":
        value = read_text(os.path.join(SYSFS_ROOT, "devices/system/cpu/cpufreq/boost"))
        return {"1": "enabled", "0": "disabled"}.get(value)
    if key == "scheduler":
        # sched_ext: "disabled", or "enabled" with ops like "lavd_1.1.3_x86_64_…".
        state = read_text(os.path.join(SYSFS_ROOT, "kernel/sched_ext/state"))
        if state == "disabled":
            return "none"
        ops = read_text(os.path.join(SYSFS_ROOT, "kernel/sched_ext/root/ops"))
        return ops.split("_", 1)[0] if state == "enabled" and ops else None
    return None


def get_power_setting(key):
    value = read_power_sysfs(key)
    if value:
        return value
    result = run(["steamosctl", POWER_SETTINGS[key][0]])
    return result.stdout.rsplit(":", 1)[-1].strip() if succeeded(result) else None


def set_power_setting(key, value):
    return succeeded(run(["steamosctl", POWER_SETTINGS[key][1], value]))


# --- display mode ----------------------------------------------------------

MODETEST_MODE = re.compile(r"^\s+#\d+\s+(\d+)x(\d+)\S*\s+([\d.]+)\s")
MODETEST_CONNECTOR = re.compile(r"^\d+\s+\d+\s+(connected|disconnected|unknown)\s+(\S+)")
MODETEST_CRTC = re.compile(r"^\d+\s+(\d+)\s+\(")


def mode_name(width, height, refresh):
    return f"{width}x{height}@{float(refresh):.2f}"


def parse_mode_name(name):
    m = re.fullmatch(r"(\d+)x(\d+)@([\d.]+)", name or "")
    return (int(m.group(1)), int(m.group(2)), float(m.group(3))) if m else None


def edid_supports_hdr(edid):
    """Whether an EDID's CTA-861 extension has an HDR static metadata block
    listing the PQ (SMPTE ST 2084) transfer function."""
    for start in range(128, len(edid) - 127, 128):
        block = edid[start : start + 128]
        if block[0] != 0x02:
            continue
        i, end = 4, min(block[2], 127)
        while i < end:
            tag, length = block[i] >> 5, block[i] & 0x1F
            # Extended tag 6: HDR static metadata; first payload byte lists EOTFs.
            if tag == 7 and length >= 2 and block[i + 1] == 0x06 and block[i + 2] & 0x04:
                return True
            i += 1 + length
    return False


def edid_identity(edid):
    """(id, name) of a display from its EDID: manufacturer and product code,
    e.g. "SAM-71B5", and the monitor name descriptor, e.g. "SAMSUNG"."""
    if len(edid) < 128 or edid[:8] != bytes.fromhex("00ffffffffffff00"):
        return None, None
    packed = int.from_bytes(edid[8:10], "big")
    maker = "".join(chr(((packed >> shift) & 0x1F) + 64) for shift in (10, 5, 0))
    display_id = f"{maker}-{int.from_bytes(edid[10:12], 'little'):04X}"
    name = None
    for offset in (54, 72, 90, 108):
        if edid[offset : offset + 3] == b"\0\0\0" and edid[offset + 3] == 0xFC:
            name = edid[offset + 5 : offset + 18].split(b"\n")[0].decode("ascii", "replace").strip()
    return display_id, name or display_id


def parse_modetest_connectors(text):
    """{connector: {"connected", "modes": [mode name, ...], "hdr_capable",
    "hdr" (scanning out in BT.2020)}} from `modetest -c`."""
    connectors, current = {}, None
    in_props, prop, edid, enums = False, None, None, ""
    for line in text.splitlines():
        m = MODETEST_CONNECTOR.match(line)
        if m:
            current = {
                "connected": m.group(1) == "connected",
                "modes": [],
                "hdr_capable": False,
                "hdr": False,
                "vrr_capable": False,
                "id": None,
                "name": None,
            }
            connectors[m.group(2)] = current
            in_props = False
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("props:"):
            in_props = True
            continue
        if in_props:
            m = re.match(r"^\d+\s+(.+):$", stripped)
            if m:
                prop, edid = m.group(1), None
            elif prop == "EDID" and stripped == "value:":
                edid = bytearray()
            elif prop == "EDID" and edid is not None and re.fullmatch(r"[0-9a-f]+", stripped):
                edid += bytes.fromhex(stripped)
                current["hdr_capable"] = edid_supports_hdr(edid)
                current["id"], current["name"] = edid_identity(edid)
            elif prop == "Colorspace" and stripped.startswith("enums:"):
                enums = stripped
            elif prop == "Colorspace" and stripped.startswith("value:"):
                value = stripped.split(":", 1)[1].strip()
                current["hdr"] = re.search(rf"\bBT2020_\w+={value}\b", enums) is not None
            elif prop == "vrr_capable" and stripped.startswith("value:"):
                current["vrr_capable"] = stripped.split(":", 1)[1].strip() == "1"
            continue
        m = MODETEST_MODE.match(line)
        if m:
            width, height, refresh = int(m.group(1)), int(m.group(2)), m.group(3)
            # Skip tiny fallback modes such as the LCD's 256x160.
            name = mode_name(width, height, refresh)
            if width >= 640 and height >= 480 and name not in current["modes"]:
                current["modes"].append(name)
    return connectors


def parse_modetest_active_modes(text):
    """Mode names of the CRTCs that are scanning out, from `modetest -p`."""
    active, lit = [], False
    for line in text.splitlines():
        if line.startswith("Planes"):
            break
        m = MODETEST_CRTC.match(line)
        if m:
            lit = m.group(1) != "0"
            continue
        m = MODETEST_MODE.match(line)
        if lit and m:
            active.append(mode_name(int(m.group(1)), int(m.group(2)), m.group(3)))
            lit = False
    return active


def is_internal_connector(name):
    return name.startswith(("eDP", "LVDS", "DSI"))


def display_info():
    """The display an app will use and its modes, read while Gaming Mode runs.

    External displays win, as in Gaming Mode. "current" is Gamescope's mode
    when it's one of the display's own."""
    if not shutil.which("modetest"):
        return None
    conns = run(["modetest", "-M", "amdgpu", "-c"])
    crtcs = run(["modetest", "-M", "amdgpu", "-p"])
    if not succeeded(conns):
        return None
    connectors = {
        name: c
        for name, c in parse_modetest_connectors(conns.stdout).items()
        if c["connected"] and c["modes"] and not name.startswith("Writeback")
    }
    if not connectors:
        return None
    external = [n for n in connectors if not is_internal_connector(n)]
    target = external[0] if external else next(iter(connectors))
    modes = connectors[target]["modes"]
    active = parse_modetest_active_modes(crtcs.stdout) if succeeded(crtcs) else []
    current = next((m for m in active if m in modes), None)
    return {
        "connector": target,
        "external": bool(external),
        "others": [n for n in connectors if n != target],
        "modes": modes,
        "current": current,
        "hdr_capable": connectors[target]["hdr_capable"],
        "hdr": connectors[target]["hdr"],
        "vrr_capable": connectors[target]["vrr_capable"],
        # Stable per display model, for per-display Moonlight settings.
        "id": connectors[target]["id"] or target,
        "name": ("Built-in screen" if is_internal_connector(target) else connectors[target]["name"] or target),
    }


def choose_display(pending, info):
    """Pick the session's display mode while still in Gaming Mode."""
    if not info:
        return
    forced = pending.get("display_mode")
    if forced and forced in info["modes"]:
        mode, hdr = forced, bool(pending.get("display_hdr"))
    else:
        if forced:
            log(f"{forced} isn't offered by {info['connector']}, matching Gaming Mode")
        mode, hdr = info["current"], info["hdr"]
    # Leave HDR alone on displays without it.
    hdr = bool(hdr) if info["hdr_capable"] else None
    # The internal panel starts in its preferred mode (listed first) anyway.
    if not info["external"] and mode == info["modes"][0]:
        mode = None
    scale = max(100, min(300, int(pending.get("display_scale", 100)))) / 100
    # External displays always get Gaming Mode's layout (external only).
    pending["display"] = {
        "connector": info["connector"],
        "mode": mode,
        "hdr": hdr,
        "scale": scale,
        "disable": info["others"] if info["external"] else [],
    }
    write_json(PENDING, pending)
    log(
        f"display: {info['connector']} at {mode or 'its default mode'}, scale {scale:g}, "
        f"HDR {'unsupported' if hdr is None else 'on' if hdr else 'off'}, "
        f"off: {pending['display']['disable'] or 'nothing'}"
    )


def kscreen_outputs():
    """{output name: {"enabled", "mode" (current mode id), "modes": {mode name: id}}}
    from `kscreen-doctor -j`."""
    result = run(["kscreen-doctor", "-j"])
    if not succeeded(result):
        return {}
    try:
        outputs = json.loads(result.stdout[result.stdout.index("{") :])["outputs"]
    except (ValueError, KeyError):
        return {}
    return {
        o.get("name"): {
            "enabled": bool(o.get("enabled")),
            "mode": str(o.get("currentModeId")),
            "hdr": o.get("hdr"),
            "scale": o.get("scale"),
            "modes": {
                mode_name(m["size"]["width"], m["size"]["height"], m["refreshRate"]): str(m["id"])
                for m in o.get("modes", [])
            },
        }
        for o in outputs
    }


def display_changes(outputs, display):
    """kscreen-doctor arguments that turn the current outputs into `display`.
    Every change makes a TV re-sync (a second or two of black), so only what
    differs is changed."""
    args = [f"output.{name}.disable" for name in display["disable"] if outputs.get(name, {}).get("enabled")]
    target = outputs.get(display["connector"], {})
    if not target.get("enabled"):
        args.append(f"output.{display['connector']}.enable")
    if display.get("mode"):
        mode_id = closest_mode_id(target.get("modes", {}), display["mode"])
        if not mode_id:
            log(f"KWin doesn't offer {display['mode']} on {display['connector']}")
        elif mode_id != target.get("mode"):
            args.append(f"output.{display['connector']}.mode.{mode_id}")
    # KWin's HDR uses BT.2020, so wide colour gamut goes with it.
    hdr = display.get("hdr")
    if hdr is not None and target.get("hdr") != hdr:
        state = "enable" if hdr else "disable"
        args += [f"output.{display['connector']}.hdr.{state}", f"output.{display['connector']}.wcg.{state}"]
    # Scaling doesn't change the display mode, so it never makes a TV re-sync.
    scale = display.get("scale")
    if scale and abs((target.get("scale") or 0) - scale) > 0.001:
        args.append(f"output.{display['connector']}.scale.{scale:g}")
    return args


def closest_mode_id(ids, wanted):
    """kscreen's refresh rates differ slightly from modetest's (59.999 vs 60.00)."""
    target = parse_mode_name(wanted)
    if not target:
        return None
    candidates = []
    for name, mode_id in ids.items():
        width, height, refresh = parse_mode_name(name)
        if (width, height) == target[:2] and abs(refresh - target[2]) < 0.05:
            candidates.append((abs(refresh - target[2]), mode_id))
    return min(candidates)[1] if candidates else None


def apply_display(display):
    """Set the session's outputs like Gaming Mode had them. KWin saves this to
    kwinoutputconfig.json, which restore() puts back."""
    if not display or not shutil.which("kscreen-doctor"):
        return
    args = display_changes(kscreen_outputs(), display)
    if not args:
        log("display already set up as wanted")
        return
    result = run(["kscreen-doctor", *args])
    log(
        f"display set: {' '.join(args)}"
        if succeeded(result)
        else f"kscreen-doctor failed: {result.stdout.strip() if result else 'not found'}"
    )


def backup_output_config():
    backup = KWIN_OUTPUT_CONFIG + ".quickscope"
    if os.path.exists(KWIN_OUTPUT_CONFIG):
        shutil.copy2(KWIN_OUTPUT_CONFIG, backup)
        record_undo("output_config", {"backup": backup})
    else:
        record_undo("output_config", {"backup": None})


def restore_output_config(entry):
    backup = entry.get("backup")
    if backup and os.path.exists(backup):
        os.replace(backup, KWIN_OUTPUT_CONFIG)
    elif not backup:
        remove(KWIN_OUTPUT_CONFIG)
    log("restored KWin's display configuration")


# --- network -----------------------------------------------------------------

# With "Force WPA Supplicant Wi-Fi backend" on, desktop Steam re-applies the
# backend when it starts, and steamos-manager does that by restarting
# NetworkManager even though nothing changed: a short Wi-Fi drop 3-5 s after
# Steam starts (SteamOS 3.9). With iwd, the default, Steam leaves it alone.
NM_RESTART_WAIT = 10
NETWORK_CONNECT_WAIT = 15
NETWORK_POLL = 0.25
# How long the app usually takes to open behind the loading screen, before its
# text moves on to the network.
APP_OPEN_TIME = 2


def wifi_backend():
    """ "iwd" (SteamOS's default) or "wpa_supplicant" (forced in Developer settings)."""
    out = run(["steamosctl", "get-wifi-backend"])
    return out.stdout.rsplit(":", 1)[-1].strip() if succeeded(out) else None


def networkmanager_pid():
    out = run(["systemctl", "show", "-p", "MainPID", "--value", "NetworkManager.service"])
    return out.stdout.strip() if succeeded(out) else None


def network_connected():
    out = run(["nmcli", "-t", "-g", "STATE", "general"])
    return succeeded(out) and out.stdout.strip().startswith("connected")


def active_wifi():
    """(device, connection, bssid) of the connected Wi-Fi, or None."""
    devices = run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"])
    if not succeeded(devices):
        return None
    for line in devices.stdout.splitlines():
        # nmcli -t escapes ":" inside fields as "\:".
        fields = re.split(r"(?<!\\):", line)
        if len(fields) == 4 and fields[1] == "wifi" and fields[2] == "connected":
            device, connection = fields[0], fields[3].replace("\\:", ":")
            aps = run(
                ["nmcli", "-t", "-f", "ACTIVE,BSSID", "device", "wifi", "list", "ifname", device, "--rescan", "no"]
            )
            for ap in aps.stdout.splitlines() if succeeded(aps) else []:
                active, _, bssid = ap.partition(":")
                if active == "yes":
                    return device, connection, bssid.replace("\\:", ":")
    return None


def set_connection_bssid(connection, bssid):
    return succeeded(run(["nmcli", "connection", "modify", connection, "802-11-wireless.bssid", bssid or ""]))


def lock_access_point():
    """Pin the Wi-Fi connection to the access point it's on, so it never roams
    or scans for another mid-stream. Takes effect on the next (re)connect.
    Returns the locked connection's name, or None."""
    unlock_access_point()  # a lock left behind by an interrupted session
    wifi = active_wifi()
    if not wifi:
        log("no Wi-Fi connection to lock")
        return None
    _, connection, bssid = wifi
    current = run(["nmcli", "-g", "802-11-wireless.bssid", "connection", "show", connection])
    original = current.stdout.strip().replace("\\:", ":") if succeeded(current) else ""
    if original:
        # The user's own lock (or ours, if unlocking failed): leave it.
        log(f"{connection} is already locked to {original}")
        return None
    write_json(WIFI_LOCK, {"connection": connection, "bssid": original})
    if not set_connection_bssid(connection, bssid):
        remove(WIFI_LOCK)
        log(f"could not lock {connection} to {bssid}")
        return None
    log(f"locked {connection} to access point {bssid}")
    return connection


def unlock_access_point():
    lock = read_json(WIFI_LOCK)
    if not lock:
        return
    if set_connection_bssid(lock["connection"], lock["bssid"]):
        remove(WIFI_LOCK)
        log(f"unlocked {lock['connection']}")
    else:
        log(f"could not unlock {lock['connection']} yet; trying again next session")


def reconnect_wifi(connection):
    return succeeded(run(["nmcli", "connection", "up", connection]))


class NetworkGate(threading.Thread):
    """Hold the loading screen until the network is settled: after desktop
    Steam's NetworkManager restart, or after reconnecting to apply the lock."""

    def __init__(self, hold, expect_restart, reconnect=None):
        super().__init__(daemon=True)
        self.hold = hold
        self.expect_restart = expect_restart
        self.reconnect = reconnect
        self.pid = networkmanager_pid()

    def run(self):
        try:
            self.wait()
        finally:
            self.hold.release("network")

    def wait(self):
        started = time.monotonic()
        if self.reconnect:
            self.hold.status("network", "Connecting to Wi-Fi…")
            if not reconnect_wifi(self.reconnect):
                log(f"reconnecting {self.reconnect} failed")
        elif self.expect_restart:
            # "Starting <app>…" while the app opens behind the loading screen.
            deadline = started + NM_RESTART_WAIT
            status = None
            while networkmanager_pid() == self.pid and time.monotonic() < deadline:
                if status is None and time.monotonic() - started >= APP_OPEN_TIME:
                    status = "Waiting for the network…"
                    self.hold.status("network", status)
                time.sleep(NETWORK_POLL)
            if networkmanager_pid() == self.pid:
                log(f"NetworkManager wasn't restarted within {NM_RESTART_WAIT}s")
                return
            self.hold.status("network", "Reconnecting to Wi-Fi…")
        deadline = time.monotonic() + NETWORK_CONNECT_WAIT
        while not network_connected() and time.monotonic() < deadline:
            time.sleep(NETWORK_POLL)
        log(f"network {'ready' if network_connected() else 'still down'} after {time.monotonic() - started:.1f}s")


# --- Moonlight settings override (opt-in) -------------------------------------

MOONLIGHT_FLATPAK = "com.moonlight_stream.Moonlight"
MOONLIGHT_CONF_NAME = os.path.join("Moonlight Game Streaming Project", "Moonlight.conf")
# Profile field -> Moonlight.conf key in [General].
MOONLIGHT_KEYS = {
    "width": "width",
    "height": "height",
    "fps": "fps",
    "bitrate": "bitrate",
    "vsync": "vsync",
    "framepacing": "framepacing",
    "hdr": "hdr",
    # 0 auto, 1 H.264, 2 HEVC, 4 AV1, 5 PyroWave (Nonary's fork).
    "codec": "videocfg",
    "yuv444": "yuv444",
    "stats": "showperfoverlay",
    "keep_awake": "keepawake",
    # Nonary's VRR fork only.
    "vrr": "enablevrr",
}
# Only Nonary's VRR fork saves this key, but upstream Moonlight keeps it after
# switching back, so it only counts when there's no binary to look at.
MOONLIGHT_FORK_KEY = "enablevrr"
MOONLIGHT_PYROWAVE = 5
# User installation first, as `flatpak run` picks it.
FLATPAK_DIRS = (os.path.join(HOME, ".local", "share", "flatpak"), "/var/lib/flatpak")

# Moonlight's getDefaultBitrate(): (pixels, factor) points, interpolated.
MOONLIGHT_BITRATE_TABLE = (
    (640 * 360, 1),
    (854 * 480, 2),
    (1280 * 720, 5),
    (1920 * 1080, 10),
    (2560 * 1440, 20),
    (3840 * 2160, 40),
)
# Nonary's fork, PyroWave: the PyroWave author's bitrate regression at his
# "good quality" level (35 dB PSNR-HVS-M-H, viewing distance twice the screen
# height), polynomial coefficients for 4:2:0 and 4:4:4.
PYROWAVE_GOOD_QUALITY = {
    False: (611.9945665176643, 87.22375978784984, -374.2744854882103, 138.525763156332,
            404.3879014230974, -64.04327593791751, -194.8035057145124, -12.904253458542499),
    True: (719.659360640365, 51.9223145866537, -522.3229212980714, 104.10383129754959,
           605.6153091742109, 87.26139899802612, -306.13390524230874, -110.39755862218644),
}  # fmt: skip


def moonlight_default_bitrate(width, height, fps, yuv444=False, codec=0, hdr=False):
    """The bitrate (kbps) Moonlight picks for these settings with "Use Default",
    as Nonary's fork does for PyroWave and upstream Moonlight for the rest."""
    pixels = width * height
    if codec == MOONLIGHT_PYROWAVE:
        # The regression covers 720p to 4K; outside that, the nearest end.
        pixels = min(max(pixels, 1280 * 720), 3840 * 2160)
        x = math.sqrt(pixels * 1e-6) - 2.0
        estimate = sum(c * x**i for i, c in enumerate(PYROWAVE_GOOD_QUALITY[bool(yuv444)]))
        kbps = int(estimate * 8e-3 * max(fps, 1) * (1.2 if hdr else 1.0) * 1000)
        return math.ceil(kbps / 5000) * 5000  # the fork's slider step
    table = MOONLIGHT_BITRATE_TABLE
    if pixels <= table[0][0]:
        factor = table[0][1]
    elif pixels >= table[-1][0]:
        factor = table[-1][1]
    else:
        i = next(i for i, (p, _) in enumerate(table) if pixels <= p)
        (p0, f0), (p1, f1) = table[i - 1], table[i]
        factor = (pixels - p0) / (p1 - p0) * (f1 - f0) + f0
    if yuv444:
        factor *= 2
    # Not linear past 60 FPS.
    frame_rate = (fps if fps <= 60 else math.sqrt(fps / 60) * 60) / 30
    return math.floor(factor * frame_rate + 0.5) * 1000  # qRound


def moonlight_conf(pending=None):
    """Moonlight's settings file: the Flatpak's, or a native install's. Without
    a launch to go by, the Flatpak's wins whenever it exists: a stale native
    file can be left over from an AppImage or an old install."""
    flatpak = os.path.join(HOME, ".var", "app", MOONLIGHT_FLATPAK, "config", MOONLIGHT_CONF_NAME)
    native = os.path.join(HOME, ".config", MOONLIGHT_CONF_NAME)
    command = (pending or {}).get("command") or ""
    if command and MOONLIGHT_FLATPAK not in command and os.path.exists(native):
        return native
    return flatpak if os.path.exists(flatpak) or not os.path.exists(native) else native


def is_appimage(path):
    """An AppImage, by its name or its magic bytes ("AI" and its type at offset 8)."""
    if not os.path.isfile(path):
        return False
    if path.lower().endswith(".appimage"):
        return True
    try:
        with open(path, "rb") as f:
            f.seek(8)
            return f.read(3) in (b"AI\x01", b"AI\x02")
    except OSError:
        return False


def command_appimage(command):
    """The AppImage a command runs, or None."""
    try:
        words = shlex.split(command or "")
    except ValueError:
        return None
    return next((w for w in words if is_appimage(w)), None)


def moonlight_binary(pending=None):
    """The Moonlight binary that goes with moonlight_conf(), None if not found.
    An AppImage's is packed inside it, compressed, so there's none to read."""
    if moonlight_conf(pending) == os.path.join(HOME, ".config", MOONLIGHT_CONF_NAME):
        if command_appimage((pending or {}).get("command")):
            return None
        return shutil.which("moonlight")
    for root in FLATPAK_DIRS:
        path = os.path.join(root, "app", MOONLIGHT_FLATPAK, "current", "active", "files", "bin", "moonlight")
        if os.path.isfile(path):
            return path
    return None


def moonlight_fork(pending=None, conf=None):
    """Whether Nonary's fork is installed: its binary knows PyroWave. Without
    a binary, whether the settings have the fork's key."""
    binary = moonlight_binary(pending)
    if binary:
        try:
            with open(binary, "rb") as f:
                return b"pyrowave" in f.read().lower()
        except OSError:
            pass
    if conf is None:
        conf = read_ini_section(moonlight_conf(pending)) or {}
    return MOONLIGHT_FORK_KEY in conf


def is_moonlight(pending):
    """The Flatpak, an AppImage or a native build: all have it in the command."""
    return "moonlight" in (pending.get("command") or "").lower()


def read_ini_section(path, section="General"):
    values, current = {}, None
    try:
        with open(path) as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("["):
                    current = line.strip("[]")
                elif current == section and "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value
    except OSError:
        return None
    return values


def write_ini_values(path, changes, section="General"):
    """Set (or, with None, remove) keys in one section, keeping everything else."""
    with open(path) as f:
        lines = f.read().split("\n")
    pending, current, out, inserted = dict(changes), None, [], False
    for line in lines:
        if line.startswith("["):
            if current == section and not inserted:
                out.extend(f"{k}={v}" for k, v in pending.items() if v is not None)
                inserted = True
            current = line.strip("[]")
        elif current == section and "=" in line:
            key = line.split("=", 1)[0]
            if key in pending:
                value = pending.pop(key)
                if value is not None:
                    out.append(f"{key}={value}")
                continue
        out.append(line)
    if not inserted:
        if current != section:
            out.append(f"[{section}]")
        out.extend(f"{k}={v}" for k, v in pending.items() if v is not None)
    write_file(path, "\n".join(out))


def moonlight_values(profile):
    """Moonlight.conf strings for a profile."""
    values = {}
    for field, key in MOONLIGHT_KEYS.items():
        if field in profile:
            value = profile[field]
            values[key] = ("true" if value else "false") if isinstance(value, bool) else str(int(value))
    return values


def override_moonlight(pending, info):
    """Swap in the user's settings for this display, while still in Gaming
    Mode. Only runs when the user turned the override on and set a profile."""
    if not pending.get("moonlight_override") or not is_moonlight(pending):
        return
    info = info or {}
    profile = (pending.get("moonlight_profiles") or {}).get(info.get("id"))
    if not profile:
        log(f"no Moonlight settings saved for {info.get('name') or 'this display'}, leaving Moonlight's own")
        return
    path = moonlight_conf(pending)
    original = read_ini_section(path)
    if original is None:
        log(f"Moonlight's settings not found at {path}")
        return
    wanted = moonlight_values(profile)
    if not moonlight_fork(pending, original):
        # Upstream Moonlight: no VRR, no PyroWave, whatever the profile says.
        wanted.pop(MOONLIGHT_KEYS["vrr"], None)
        if wanted.get(MOONLIGHT_KEYS["codec"]) == str(MOONLIGHT_PYROWAVE):
            del wanted[MOONLIGHT_KEYS["codec"]]
    if profile.get("auto_bitrate"):
        # Moonlight's default for what it will actually use, own values included.
        def value(field, fallback):
            key = MOONLIGHT_KEYS[field]
            return wanted.get(key, original.get(key)) or fallback

        def flag(field):
            return value(field, "false") == "true"

        try:
            wanted[MOONLIGHT_KEYS["bitrate"]] = str(
                moonlight_default_bitrate(
                    int(value("width", "1280")),
                    int(value("height", "720")),
                    int(value("fps", "60")),
                    flag("yuv444"),
                    int(value("codec", "0")),
                    flag("hdr"),
                )
            )
        except ValueError:
            log("Moonlight's settings have an unreadable value, keeping the profile's bitrate")
    changes = {k: v for k, v in wanted.items() if original.get(k) != v}
    if not changes:
        log(f"Moonlight already set up for {info.get('name')}")
        return
    record_undo("moonlight", {"path": path, "set": changes, "original": {k: original.get(k) for k in changes}})
    write_ini_values(path, changes)
    log(f"Moonlight settings for {info.get('name')}: {changes}")


def restore_moonlight(entry):
    """Put Moonlight's own values back for every overridden key. With the
    override on, the profile is what Moonlight uses, so changes made to those
    keys in Moonlight during the session don't carry over. Other keys are
    never touched."""
    if read_ini_section(entry["path"]) is None:
        return
    write_ini_values(entry["path"], entry["original"])
    log(f"restored Moonlight settings {sorted(entry['original'])}")


def moonlight_hosts(path=None):
    """Hosts and their apps as Moonlight last saw them, from its settings:
    [{"name", "uuid", "apps": [{"id", "name"}], "hidden": [id]}]. Addresses
    and certificates are never read out. Apps hidden in Moonlight are left out
    of "apps"; their ids are kept so a fresh list from the host can skip them."""
    hosts = {}
    try:
        with open(path or moonlight_conf()) as f:
            section = None
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("["):
                    section = line
                    continue
                if section != "[hosts]":
                    continue
                m = re.match(r"(\d+)\\(hostname|uuid)=(.*)", line)
                if m:
                    hosts.setdefault(m.group(1), {"apps": {}})[m.group(2)] = m.group(3)
                    continue
                m = re.match(r"(\d+)\\apps\\(\d+)\\(name|id|hidden)=(.*)", line)
                if m:
                    app = hosts.setdefault(m.group(1), {"apps": {}})["apps"].setdefault(m.group(2), {})
                    app[m.group(3)] = m.group(4)
    except OSError:
        return []
    result = []
    for host in hosts.values():
        if not host.get("hostname"):
            continue
        apps = [
            {"id": int(a["id"]) if a.get("id", "").isdigit() else 0, "name": unquote_ini(a["name"])}
            for a in host["apps"].values()
            if a.get("name") and a.get("hidden") != "true"
        ]
        hidden = [
            int(a["id"]) for a in host["apps"].values() if a.get("hidden") == "true" and a.get("id", "").isdigit()
        ]
        result.append({"name": host["hostname"], "uuid": host.get("uuid", ""), "apps": apps, "hidden": hidden})
    return result


def unquote_ini(value):
    """QSettings quotes values with leading/trailing spaces; keep the spaces."""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


INI_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "0": "\0", "\\": "\\", '"': '"'}


def ini_bytes(value):
    """A QSettings @ByteArray(...) value (quoted or not) as text, e.g. a PEM."""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    m = re.fullmatch(r"@ByteArray\((.*)\)", value, re.S)
    if not m:
        return None
    return re.sub(
        r"\\(x[0-9a-fA-F]{1,2}|.)",
        lambda e: chr(int(e.group(1)[1:], 16)) if e.group(1)[0] == "x" else INI_ESCAPES.get(e.group(1), e.group(1)),
        m.group(1),
    )


def moonlight_host_entry(path, name):
    """One saved host's raw keys from Moonlight's [hosts] section, or None."""
    hosts = {}
    try:
        with open(path) as f:
            section = None
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("["):
                    section = line
                elif section == "[hosts]" and (m := re.match(r"(\d+)\\([^\\=]+)=(.*)", line)):
                    hosts.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
    except OSError:
        return None
    return next((h for h in hosts.values() if h.get("hostname") == name), None)


# --- Starting the host's app early ---------------------------------------------
#
# Starting a stream is two steps: the host starts the app (GameStream's
# /launch, which can take seconds, e.g. while Apollo sets up a virtual display),
# then the client connects to it. Moonlight resumes an app the host is already
# running, so the backend starts it as the A press switches sessions, and the
# session waits for that before running Moonlight. The request is Moonlight's
# own: its pairing certificate (read, never copied to disk) and its settings.

HOST_APP = os.path.join(STATE, "host_app.json")
# The session waits at most this long for the host to finish starting the app.
HOST_APP_WAIT = 20
HOST_HTTP_TIMEOUT = 3
# Apps can take a while to start; the host answers once it has.
HOST_LAUNCH_TIMEOUT = 30
# Moonlight's audiocfg -> GameStream's surroundAudioInfo, (channel mask << 16)
# | channel count: stereo, 5.1, 7.1.
SURROUND_AUDIO_INFO = {"0": (0x3 << 16) | 2, "1": (0x3F << 16) | 6, "2": (0x63F << 16) | 8}


class HostRequestError(Exception):
    pass


class MoonlightClient:
    """GameStream requests to one host, as Moonlight makes them."""

    def __init__(self, conf, name):
        general = read_ini_section(conf) or {}
        host = moonlight_host_entry(conf, name)
        if host is None:
            raise HostRequestError(f"{name} isn't one of Moonlight's hosts")
        self.cert = ini_bytes(general.get("certificate", ""))
        self.key = ini_bytes(general.get("key", ""))
        self.server_cert = ini_bytes(host.get("srvcert", ""))
        if not (self.cert and self.key and self.server_cert):
            raise HostRequestError(f"Moonlight isn't paired with {name}")
        self.unique_id = general.get("uniqueid") or "0123456789ABCDEF"
        self.settings = general
        # Moonlight's order: the LAN address first.
        self.addresses = []
        for kind in ("local", "manual", "remote", "ipv6"):
            address = host.get(f"{kind}address", "").strip("[]")
            port = host.get(f"{kind}port", "")
            if address:
                self.addresses.append((address, int(port) if port.isdigit() and int(port) else 47989))
        self.address = self.https_port = None

    def query(self, params=None):
        return "&".join(
            f"{k}={v}" for k, v in {"uniqueid": self.unique_id, "uuid": uuid_hex(), **(params or {})}.items()
        )

    def find(self):
        """The first address that answers, and its HTTPS port."""
        import http.client

        for address, port in self.addresses:
            conn = http.client.HTTPConnection(address, port, timeout=HOST_HTTP_TIMEOUT)
            try:
                conn.request("GET", f"/serverinfo?{self.query()}")
                info = parse_host_xml(conn.getresponse().read())
            except (OSError, HostRequestError, http.client.HTTPException):
                continue
            finally:
                conn.close()
            https_port = info.findtext("HttpsPort") or ""
            self.address = address
            self.https_port = int(https_port) if https_port.isdigit() else port - 5
            return True
        return False

    def request(self, path, params=None, timeout=HOST_HTTP_TIMEOUT):
        """An HTTPS request with Moonlight's certificate, to the host whose
        certificate Moonlight pinned when pairing. The parsed XML."""
        import http.client
        import ssl

        if self.address is None and not self.find():
            raise HostRequestError("host not reachable")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # The host's certificate is self-signed; it's checked against the
        # pinned one below instead.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        # In memory only: an anonymous file, gone when closed.
        fd = os.memfd_create("quickscope-client", os.MFD_CLOEXEC)
        try:
            os.write(fd, (self.cert + "\n" + self.key).encode())
            context.load_cert_chain(f"/proc/self/fd/{fd}")
        finally:
            os.close(fd)
        conn = http.client.HTTPSConnection(self.address, self.https_port, timeout=timeout, context=context)
        try:
            conn.connect()
            if conn.sock.getpeercert(binary_form=True) != ssl.PEM_cert_to_DER_cert(self.server_cert):
                raise HostRequestError("the host's certificate isn't the one Moonlight paired with")
            conn.request("GET", f"/{path}?{self.query(params)}")
            return parse_host_xml(conn.getresponse().read())
        except (OSError, http.client.HTTPException) as e:
            raise HostRequestError(f"{path}: {e}") from e
        finally:
            conn.close()

    def launch_params(self, appid):
        """/launch's parameters as Moonlight sends them for its current settings."""
        s = self.settings
        params = {
            "appid": appid,
            "mode": f"{int(s['width'])}x{int(s['height'])}x{int(s['fps'])}",
            "additionalStates": 1,
            "sops": 0 if s.get("gameopts") == "false" else 1,
            # The host's stream keys; Moonlight's resume replaces them.
            "rikey": os.urandom(16).hex(),
            "rikeyid": int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF,
            "localAudioPlayMode": 1 if s.get("hostaudio") == "true" else 0,
            "surroundAudioInfo": SURROUND_AUDIO_INFO.get(s.get("audiocfg", "0"), SURROUND_AUDIO_INFO["0"]),
            "remoteControllersBitmap": 1,
            "gcmap": 1,
            "psmap": 0,
            "gcpersist": 0,
            "corever": 1,
        }
        if s.get("hdr") == "true":
            params.update(
                hdrMode=1,
                clientHdrCapVersion=0,
                clientHdrCapSupportedFlagsInUint32=0,
                clientHdrCapMetaDataId="NV_STATIC_METADATA_TYPE_1",
                clientHdrCapDisplayData="0x0x0x0x0x0x0x0x0x0x0",
            )
        return params


def uuid_hex():
    import uuid

    return uuid.uuid4().hex


def parse_host_xml(data):
    """A GameStream reply's root element; HostRequestError if it's an error."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise HostRequestError(f"unreadable reply: {e}") from e
    status = root.get("status_code", "200")
    if status != "200":
        raise HostRequestError(f"{root.get('status_message') or 'error'} ({status})")
    return root


def write_host_app(state, pending):
    """The early start's state, with what quitting the app again needs."""
    record = {
        "state": state,
        "created": pending.get("created"),
        "pid": os.getpid(),
        "host": (pending.get("stream") or {}).get("host"),
        "conf": moonlight_conf(pending),
    }
    # Whole or not at all: the session may be reading it right now.
    write_file(HOST_APP + ".tmp", json.dumps(record))
    os.replace(HOST_APP + ".tmp", HOST_APP)


def start_host_app():
    """Start the staged stream's app on the host (run by the Decky backend
    right after --prepare, in Gaming Mode). Leaves the outcome in HOST_APP."""
    try:
        with open(PENDING) as f:
            pending = json.load(f)
    except (OSError, ValueError):
        return 1
    stream = pending.get("stream") or {}
    appid = pending.get("appid")
    if not stream.get("host") or not appid:
        return 1
    write_host_app("starting", pending)
    started = time.monotonic()
    try:
        client = MoonlightClient(moonlight_conf(pending), stream["host"])
        current = client.request("serverinfo").findtext("currentgame") or "0"
        if current == str(appid):
            state, note = "running", "already running"
        elif current != "0":
            # Moonlight will ask whether to quit it.
            state, note = "busy", f"another app ({current}) is running"
        else:
            client.request("launch", client.launch_params(appid), timeout=HOST_LAUNCH_TIMEOUT)
            state, note = "started", "started"
    except (HostRequestError, KeyError, ValueError) as e:
        state, note = "failed", f"couldn't start it: {e}"
    log(f"host app {stream.get('app', '').strip()!r} on {stream['host']}: {note} ({time.monotonic() - started:.1f}s)")
    write_host_app(state, pending)
    return 0


def read_host_app():
    try:
        with open(HOST_APP) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def wait_for_host_app(created, on_wait=None):
    """The early start's record for the launch staged at `created`, once it
    has an outcome (None if there was none). Before Moonlight runs: so it
    finds the app running and resumes it instead of starting it again."""
    deadline = time.monotonic() + HOST_APP_WAIT
    waited = False
    while True:
        host_app = read_host_app()
        if host_app is None or host_app.get("created") != created:
            return None  # none, or from another launch
        if host_app.get("state") != "starting" or not pid_alive(host_app.get("pid")):
            break
        if time.monotonic() >= deadline:
            log("host still starting the app, going ahead anyway")
            break
        if not waited and on_wait:
            on_wait()
        waited = True
        time.sleep(0.1)
    if waited:
        log(f"waited {HOST_APP_WAIT - (deadline - time.monotonic()):.1f}s for the host: {host_app.get('state')}")
    return host_app


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def quit_host_app():
    """Undo an early start whose session never came (the launch was cancelled)."""
    host_app = read_host_app()
    if host_app is None:
        return 0
    # Quitting before the host has finished starting it would be too early.
    host_app = wait_for_host_app(host_app.get("created")) or {}
    remove(HOST_APP)
    if host_app.get("state") != "started":
        return 0
    try:
        MoonlightClient(host_app["conf"], host_app["host"]).request("cancel")
        log("launch cancelled, quit the app on the host")
    except (HostRequestError, KeyError, TypeError) as e:
        log(f"launch cancelled, couldn't quit the app on the host: {e}")
    return 0


# Moonlight's output while a `stream` launch connects. The loading screen
# shows the same steps Moonlight's own screen would.
STREAM_HOST_FOUND = re.compile(r'Qt Info: ".*" is now online at')
# Asking the host to start (or resume) the app, or to quit the one it's running.
STREAM_REQUEST = re.compile(r'Executing request: "https?://[^/]+/(launch|resume|cancel)\?')
# Connection steps as Moonlight logs them, and as its screen names them
# ("Starting RTSP handshake...").
STREAM_STEP = re.compile(r"SDL Info \(\d+\): ((?:Initializing|Resolving|Starting) [\w ]+?)\.\.\.$")
STREAM_STEP_NAMES = {
    "Initializing platform": "platform initialization",
    "Resolving host name": "name resolution",
    "Initializing audio stream": "audio stream initialization",
    "Starting RTSP handshake": "RTSP handshake",
    "Initializing control stream": "control stream initialization",
    "Initializing video stream": "video stream initialization",
    "Initializing input stream": "input stream initialization",
    "Starting control stream": "control stream establishment",
    "Starting video stream": "video stream establishment",
    "Starting audio stream": "audio stream establishment",
    "Starting input stream": "input stream establishment",
}
STREAM_LAST_STEP = "Starting input stream"
STREAM_STARTED = re.compile(r"Video stream is \d+x\d+")
# With another app running on the host, Moonlight asks whether to quit it
# (nothing in its output says so). If it hasn't asked the host for the app
# this long after finding it, step aside so its question can be seen.
STREAM_QUESTION_WAIT = 3
# The picture, about 0.7 s after the stream starts; until then Moonlight shows
# its own loading screen (its last connection steps).
STREAM_FIRST_VIDEO = "Received first video packet"
# The user quitting the stream with Moonlight's combo (Start+Select+L1+R1, or
# Ctrl+Alt+Shift+Q): Moonlight exits about 0.2 s later.
STREAM_QUIT = re.compile(r"Detected quit (?:gamepad button|key) combo")
STREAM_FAILED = re.compile(r"Qt Critical:")
STREAM_TERMINATED = re.compile(r"Connection terminated: (-?\d+)")
# Longest the loading screen waits for video before showing Moonlight anyway.
STREAM_WAIT = 45
# Moonlight's termination codes that a reconnect wouldn't help: 0 is the host
# ending the stream on purpose (e.g. the app quit), -103 protected content,
# -104 a fatal encoder error. Anything else, like -1 (connection lost), is a
# drop worth reconnecting.
STREAM_NO_RECONNECT = {0, -103, -104}
# How long to keep trying to reconnect a dropped stream, and the pause first.
RECONNECT_WINDOW = 180
RECONNECT_DELAY = 2


class LoadingHold:
    """Closes the loading screen once every reason to keep it up has gone,
    e.g. the network settling and the stream starting. Like Moonlight's own
    screen, the headline (`title`) stays put and the step under way shows
    beneath it."""

    def __init__(self, loading, reasons, title=""):
        self.loading = loading
        self.title = title
        # Each reason's latest step ("" keeps the current one).
        self.reasons = dict.fromkeys(reasons, "")
        self.lock = threading.Lock()

    def text(self, step):
        return f"{self.title}\n{step}" if self.title else step

    def status(self, reason, step):
        with self.lock:
            if reason not in self.reasons:
                return
            self.reasons[reason] = step
        write_file(LOADING_STATUS, self.text(step))

    def release(self, reason):
        with self.lock:
            if reason not in self.reasons:
                return  # already released (e.g. video started, then Moonlight exited)
            del self.reasons[reason]
            remaining = [step for step in self.reasons.values() if step]
            if self.reasons:
                if remaining:
                    write_file(LOADING_STATUS, self.text(remaining[-1]))
                return
        write_file(LOADING_STATUS, "")
        close_loading_screen(self.loading)


def plugged_in():
    """True on mains power, or on a device without a battery."""
    has_battery = False
    for supply in glob.glob(os.path.join(POWER_SUPPLY_ROOT, "*")):
        kind = read_text(os.path.join(supply, "type"))
        if kind == "Mains" and read_text(os.path.join(supply, "online")) == "1":
            return True
        has_battery = has_battery or kind == "Battery"
    return not has_battery


def battery_dir():
    for supply in sorted(glob.glob(os.path.join(POWER_SUPPLY_ROOT, "*"))):
        if read_text(os.path.join(supply, "type")) == "Battery":
            return supply
    return None


def battery_status():
    """(percent, discharging) of the first battery, or None without one."""
    supply = battery_dir()
    capacity = supply and read_text(os.path.join(supply, "capacity"))
    if capacity and capacity.isdigit():
        return int(capacity), read_text(os.path.join(supply, "status")) == "Discharging"
    return None


def battery_charge():
    """(status, charge now, full charge, current) of the first battery: µAh
    and µA (the Deck), or µWh and µW where only energy is reported. None
    without one."""
    supply = battery_dir()
    if not supply:
        return None
    for keys in (("charge_now", "charge_full", "current_now"), ("energy_now", "energy_full", "power_now")):
        values = [read_text(os.path.join(supply, key)) for key in keys]
        if all(v and v.lstrip("-").isdigit() for v in values):
            now, full, rate = (abs(int(v)) for v in values)
            return read_text(os.path.join(supply, "status")), now, full, rate
    return None


def format_duration(hours):
    hours, minutes = divmod(round(hours * 60), 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes} min"


def battery_summary(pct, status, now, full, rate):
    """(label, value) for the battery on the OSD: time left on battery, or
    time to full while charging. `rate` is an average current (or power)."""
    if status == "Discharging":
        return f"Battery {pct}%", f"{format_duration(now / rate)} left" if rate else ""
    if status == "Charging":
        return f"Charging {pct}%", f"Full in {format_duration((full - now) / rate)}" if rate and full > now else ""
    return f"Battery {pct}%", "Full" if status == "Full" else "Plugged in"


class BatteryWarning(threading.Thread):
    """Warns on the OSD as the battery runs low. Gaming Mode does, but nothing
    in the session would: KDE's warning is a notification, and there's no
    Plasma shell to show it. Each level warns once per discharge."""

    def __init__(self, osd):
        super().__init__(daemon=True)
        self.osd = osd
        self.warned = set()
        # Recent currents, for a steadier estimate than one reading: the
        # Deck's jumps by 10% from second to second.
        self.samples = collections.deque(maxlen=BATTERY_SAMPLES)
        self.sampled_status = None
        self.stopped = threading.Event()

    def run(self):
        self.sample()
        while not self.stopped.wait(BATTERY_POLL):
            self.check()

    def sample(self, record=True):
        """The current reading, with its rate averaged over recent samples.
        `record` adds it to them (the regular poll, not a tap)."""
        charge = battery_charge()
        if not charge:
            return None
        status, now, full, rate = charge
        if status != self.sampled_status:
            # Charging and discharging rates don't mix.
            self.samples.clear()
            self.sampled_status = status
        rates = [*self.samples, rate]
        if record:
            self.samples.append(rate)
        return status, now, full, sum(rates) / len(rates)

    def show_status(self):
        """The battery and its projected time, on a tap of "…"."""
        status = battery_status()
        charge = self.sample(record=False)
        if not status or not charge:
            return
        label, value = battery_summary(status[0], *charge)
        self.osd.show(label, status[0] / 100, value, duration=BATTERY_STATUS_TIME)

    def check(self):
        self.sample()
        status = battery_status()
        if not status:
            return
        pct, discharging = status
        if not discharging:
            self.warned.clear()
            return
        due = {level for level in BATTERY_WARN_LEVELS if pct <= level} - self.warned
        if due:
            self.warned |= due
            label = "Battery critical" if pct <= BATTERY_CRITICAL else "Battery low"
            log(f"{label.lower()}: {pct}%")
            self.osd.show(label, pct / 100, f"{pct}%", duration=BATTERY_WARNING_TIME)

    def stop(self):
        self.stopped.set()


def resolve_power_profile(name):
    if name == "auto":
        return AUTO_PROFILES["plugged_in" if plugged_in() else "on_battery"]
    return name


def apply_power_profile(name, extra=None):
    """Apply a POWER_PROFILES entry, plus `extra` settings (e.g. Wi-Fi power
    saving), for the session. Returns the settings that took, for the keeper
    to hold."""
    profile = POWER_PROFILES.get(name)
    if not profile or not shutil.which("steamosctl"):
        return {}
    profile = {**profile, **(extra or {})}
    before, applied = {}, {}
    for key, value in profile.items():
        current = get_power_setting(key)
        if current is not None and set_power_setting(key, value):
            before[key] = current
            applied[key] = value
    if before:
        record_undo("performance", before)
        log(f"{name} profile on (was {before})")
    return applied


# --- undo bookkeeping --------------------------------------------------------


def record_undo(key, value):
    undo = read_json(UNDO) or {}
    undo[key] = value
    write_json(UNDO, undo)


def restore(reload=True, restart_shell=False):
    """Undo every one-shot tweak recorded in undo.json. Safe to run repeatedly."""
    undo = read_json(UNDO) or {}
    remove(UNDO)
    for entry in undo.get("hidden_autostart", []):
        remove(entry["path"])
        if entry.get("backup") and os.path.exists(entry["backup"]):
            os.replace(entry["backup"], entry["path"])
        log(f"restored {entry['path']}")
    masked = undo.get("masked", [])
    # One call (one systemd reload) for all of them, unit by unit only if that fails.
    if masked and not succeeded(systemctl("unmask", "--runtime", *masked)):
        for unit in masked:
            if not succeeded(systemctl("unmask", "--runtime", unit)):
                log(f"failed to unmask {unit}")
    if restart_shell and PLASMASHELL_UNIT in masked:
        systemctl("start", "--no-block", PLASMASHELL_UNIT)
    if "splash_engine" in undo:
        restore_splash(undo["splash_engine"])
    if "powerdevil" in undo:
        restore_idle(undo["powerdevil"])
    unlock_access_point()
    if "moonlight" in undo:
        restore_moonlight(undo["moonlight"])
    if "output_config" in undo:
        restore_output_config(undo["output_config"])
    if "brightness" in undo and write_backlight(undo["brightness"]["raw"]):
        log(f"restored brightness {undo['brightness']['raw']}")
    perf = undo.get("performance", {})
    for key, value in perf.items():
        if key in POWER_SETTINGS and value:
            set_power_setting(key, value)
    if perf:
        log(f"restored performance settings {perf}")
    remove(ONESHOT_AUTOSTART)
    if reload:
        systemctl("daemon-reload")


# --- prepare (Gaming Mode, before the switch) --------------------------------


def install_unit():
    unit_path = os.path.join(UNIT_DIR, UNIT_NAME)
    write_file(
        unit_path,
        UNIT_TEMPLATE.format(
            pending=PENDING, python=python_exe(), launcher=SELF, target=UNIT_TARGET, recover=RECOVER_UNIT_NAME
        ),
    )
    write_file(
        os.path.join(UNIT_DIR, RECOVER_UNIT_NAME), RECOVER_UNIT_TEMPLATE.format(python=python_exe(), launcher=SELF)
    )
    link = os.path.join(UNIT_DIR, f"{UNIT_TARGET}.wants", UNIT_NAME)
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(unit_path, link)


def read_desktop_entry(path):
    entry, in_section = {}, False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith("["):
            in_section = line == "[Desktop Entry]"
        elif in_section and "=" in line:
            key, value = line.split("=", 1)
            entry.setdefault(key.strip(), value.strip())
    return entry


def is_steam_entry(entry):
    try:
        argv = shlex.split(entry.get("Exec", ""))
    except ValueError:
        return False
    return bool(argv) and (os.path.basename(argv[0]) == "steam" or "com.valvesoftware.Steam" in argv)


def hide_autostart(should_hide):
    """Shadow matching autostart entries with Hidden=true user overrides.

    should_hide(name, entry, is_user_entry) decides per entry.
    """
    config_dirs = os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg"
    search = [AUTOSTART_DIR] + [os.path.join(d, "autostart") for d in config_dirs.split(":") if d]
    names = {os.path.basename(p) for d in search for p in glob.glob(os.path.join(d, "*.desktop"))}
    names.discard(os.path.basename(ONESHOT_AUTOSTART))
    hidden = []
    for name in sorted(names):
        # User entries shadow system entries with the same file name.
        effective = next((p for p in (os.path.join(d, name) for d in search) if os.path.exists(p)), None)
        entry = read_desktop_entry(effective) if effective else None
        if not entry or entry.get("Hidden", "").lower() == "true":
            continue
        if not should_hide(name, entry, os.path.dirname(effective) == AUTOSTART_DIR):
            continue
        user_path = os.path.join(AUTOSTART_DIR, name)
        backup = None
        if os.path.exists(user_path):
            backup = user_path + ".quickscope-bak"
            os.replace(user_path, backup)
        write_file(user_path, HIDDEN_ENTRY)
        hidden.append({"path": user_path, "backup": backup})
        # Record as we go so a failure part-way through is still undoable.
        record_undo("hidden_autostart", hidden)
        log(f"hid autostart {effective}")
    return hidden


def kconfig_tool(kind):
    """kreadconfig6/kwriteconfig6 (or the Plasma 5 names)."""
    return shutil.which(f"k{kind}config6") or shutil.which(f"k{kind}config5")


def disable_splash():
    """Turn the Plasma splash off for this session only.

    Plasma starts the splash over D-Bus activation, not through
    plasma-ksplash.service, so masking that unit doesn't stop it. KDE's own
    switch is ksplashrc's Engine=none; the original value is restored after.
    """
    read, write = kconfig_tool("read"), kconfig_tool("write")
    if not (read and write):
        log("kreadconfig/kwriteconfig not found, leaving the splash on")
        return
    current = run([read, "--file", "ksplashrc", "--group", "KSplash", "--key", "Engine"])
    if not succeeded(current):
        return
    original = current.stdout.strip() or None  # None: key wasn't set
    if original == "none":
        return
    file_existed = os.path.exists(KSPLASHRC)
    if succeeded(run([write, "--file", "ksplashrc", "--group", "KSplash", "--key", "Engine", "none"])):
        record_undo("splash_engine", {"value": original, "file_existed": file_existed})
        log(f"splash off for this session (Engine was {original!r})")


def restore_splash(undo):
    original = undo.get("value")
    if not undo.get("file_existed", True):
        # We created ksplashrc just for this session.
        remove(KSPLASHRC)
        log("removed session-only ksplashrc")
        return
    write = kconfig_tool("write")
    if not write:
        return
    if original is None:
        run([write, "--file", "ksplashrc", "--group", "KSplash", "--key", "Engine", "--delete"])
    else:
        run([write, "--file", "ksplashrc", "--group", "KSplash", "--key", "Engine", original])
    log(f"restored splash Engine {original!r}")


def idle_overrides(pending):
    """powerdevilrc values for the idle settings, as (profile, group, key,
    value). Minutes; 0 is never, None (or a negative) leaves SteamOS's own:
    on battery, screen off after 1 minute and sleep after 5."""
    values = []
    sleep, screen = pending.get("idle_sleep_min"), pending.get("idle_screen_off_min")
    for profile in POWERDEVIL_PROFILES:
        if sleep is not None and sleep >= 0:
            values.append((profile, "SuspendAndShutdown", "AutoSuspendAction", "1" if sleep else "0"))
            if sleep:
                values.append((profile, "SuspendAndShutdown", "AutoSuspendIdleTimeoutSec", str(sleep * 60)))
        if screen is not None and screen >= 0:
            values.append((profile, "Display", "TurnOffDisplayWhenIdle", "true" if screen else "false"))
            if screen:
                values.append((profile, "Display", "TurnOffDisplayIdleTimeoutSec", str(screen * 60)))
    return values


def override_idle(pending):
    """Set KDE's idle timers for this session only, before Plasma starts and
    reads them. The user's own powerdevilrc (if any) is put back afterwards."""
    values = idle_overrides(pending)
    if not values:
        return
    write = kconfig_tool("write")
    if not write:
        log("kwriteconfig not found, leaving KDE's idle settings alone")
        return
    backup = None
    if os.path.exists(POWERDEVILRC):
        backup = POWERDEVILRC + ".quickscope-bak"
        shutil.copyfile(POWERDEVILRC, backup)
    record_undo("powerdevil", {"backup": backup})
    for profile, group, key, value in values:
        run([write, "--file", "powerdevilrc", "--group", profile, "--group", group, "--key", key, value])
    sleep, screen = pending.get("idle_sleep_min"), pending.get("idle_screen_off_min")
    log(f"idle settings for this session (minutes, 0 never): sleep {sleep}, screen off {screen}")


def restore_idle(undo):
    backup = undo.get("backup")
    if backup and os.path.exists(backup):
        os.replace(backup, POWERDEVILRC)
    else:
        remove(POWERDEVILRC)  # created just for this session
    log("restored KDE's idle settings")


def choose_brightness(pending):
    """Decide the session's starting brightness while still in Gaming Mode,
    where the backlight is at the level the user chose there."""
    current = read_backlight()
    if not current:
        log("no backlight found, leaving brightness alone")
        return
    raw, max_raw = current
    if pending.get("match_gaming_brightness", True):
        target = raw
    else:
        target = brightness_pct_to_raw(pending.get("brightness_pct", 50), max_raw)
    pending["brightness_raw"] = target
    write_json(PENDING, pending)
    record_undo("brightness", {"raw": raw})
    log(f"session brightness {target} (Gaming Mode had {raw} of {max_raw})")


def prepare():
    trim_log()
    restore(reload=False)
    pending = read_json(PENDING)
    if pending is None:
        print("no staged launch")
        return 1

    install_unit()
    write_file(ONESHOT_AUTOSTART, AUTOSTART_TEMPLATE.format(python=python_exe(), launcher=SELF))

    # Every launch method either starts Steam itself (Steam games, hybrid) or
    # deliberately runs without it (direct), so desktop Steam's own autostart
    # is never wanted: it would race our Steam or grab the controller.
    hide_autostart(lambda name, entry, is_user: is_steam_entry(entry) or (not is_user and name in QUIET_AUTOSTART))

    # The splash waits for the Plasma panel, which the session skips, so it
    # would sit there until it times out.
    disable_splash()
    override_idle(pending)

    choose_brightness(pending)
    # Read once: modetest is on the launch path.
    display = display_info()
    choose_display(pending, display)
    override_moonlight(pending, display)

    # One call for all units: each systemctl mask reloads systemd (~0.5 s),
    # and that reload also picks up the unit installed above. Recorded first,
    # so an interrupted prepare still gets them unmasked.
    units = [PLASMASHELL_UNIT, *QUIET_MASKED_UNITS]
    record_undo("masked", units)
    if succeeded(systemctl("mask", "--runtime", *units)):
        log(f"masked {', '.join(units)} for this session")
    else:
        for unit in units:
            if not succeeded(systemctl("mask", "--runtime", unit)):
                log(f"failed to mask {unit}")
        reload = systemctl("daemon-reload")
        if not succeeded(reload):
            print(f"systemctl --user daemon-reload failed: {reload.stdout.strip() if reload else 'not found'}")
            return 1
    log(f"prepared {pending.get('name')} ({pending.get('mode')})")
    return 0


# --- launch (inside the desktop session) -------------------------------------


def claim_pending():
    """Atomically take the staged launch so the unit and autostart can't both run it."""
    claimed = f"{PENDING}.{os.getpid()}"
    try:
        os.rename(PENDING, claimed)
    except FileNotFoundError:
        return None
    remove(ONESHOT_AUTOSTART)
    try:
        pending = read_json(claimed)
    finally:
        remove(claimed)
    if pending is None:
        return None
    age = time.time() - pending.get("created", 0)
    if age > PENDING_MAX_AGE:
        log(f"ignoring stale launch ({int(age)}s old)")
        restore()
        return None
    return pending


def wait_for_kwin():
    deadline = time.monotonic() + KWIN_TIMEOUT
    while time.monotonic() < deadline:
        out = dbus(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus.NameHasOwner", "string:org.kde.KWin"
        )
        if out and "boolean true" in out:
            return True
        time.sleep(0.1)
    return False


def load_session_script(force_fullscreen, loading, minimize_steam_windows=False, hold_loading=False):
    """Load the KWin script that fullscreens/activates the app's first window
    and closes the loading screen once that window appears."""
    script = (
        SESSION_JS.replace("%LOADING_TITLE%", LOADING_TITLE)
        .replace("%OSD_TITLE%", OSD_TITLE)
        .replace("%LOADING_PID%", str(loading.pid if loading else -1))
        .replace("%FORCE_FULLSCREEN%", "true" if force_fullscreen else "false")
        .replace("%MINIMIZE_STEAM_WINDOWS%", "true" if minimize_steam_windows else "false")
        .replace("%HOLD_LOADING%", "true" if hold_loading else "false")
    )
    write_file(KWIN_SCRIPT, script)
    dbus("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", f"string:{KWIN_SCRIPT_NAME}")
    out = dbus(
        "org.kde.KWin",
        "/Scripting",
        "org.kde.kwin.Scripting.loadScript",
        f"string:{KWIN_SCRIPT}",
        f"string:{KWIN_SCRIPT_NAME}",
    )
    match = re.search(r"int32 (-?\d+)", out or "")
    if not match or int(match.group(1)) < 0:
        log("could not load KWin session script")
        return False
    script_id = match.group(1)
    # Plasma 6 path first, Plasma 5 second.
    for path in (f"/Scripting/Script{script_id}", f"/{script_id}"):
        if dbus("org.kde.KWin", path, "org.kde.kwin.Script.run") is not None:
            log("KWin session script running")
            return True
    log("could not run KWin session script")
    return False


def show_loading_screen(message, still=False, overlay=False):
    """A full-screen loading screen with `message`. Returns the process or None.

    `still` swaps the spinner for an underline: the returning screen can stay
    frozen on display after KWin exits (whether it does varies), and a spinner
    stopped mid-turn would look like a hang. An `overlay` stays above every
    window, and only the launcher closes it (the KWin script can't)."""
    qml = shutil.which("qml6") or shutil.which("qml")
    if not qml:
        log("no qml runtime, skipping loading screen")
        return None
    write_file(LOADING_STATUS, "")
    screen = LOADING_QML % {
        "title": json.dumps(LOADING_TITLE),
        "message": json.dumps(message),
        "status_url": json.dumps("file://" + LOADING_STATUS),
        "spinner": "false" if still else "true",
        "imports": LOADING_OVERLAY_IMPORTS if overlay else "",
        "placement": LOADING_OVERLAY if overlay else LOADING_WINDOW,
    }
    write_file(LOADING_SCREEN, screen)
    env = dict(os.environ, QML_XHR_ALLOW_FILE_READ="1")
    if overlay:
        env["QT_WAYLAND_SHELL_INTEGRATION"] = "layer-shell"
    try:
        return subprocess.Popen([qml, LOADING_SCREEN], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log(f"loading screen failed: {e}")
        return None


def close_loading_screen(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


class ReturningScreen:
    """The "Returning to Gaming Mode…" screen, shown once: as soon as the user quits the
    stream (before Moonlight has closed its windows), or when the app exits.
    An overlay, so nothing closing underneath shows through; the logout
    closes it."""

    MESSAGE = "Returning to Gaming Mode…"

    def __init__(self, enabled):
        self.enabled = enabled
        self.shown = False
        self.lock = threading.Lock()
        self.reveal = threading.Event()
        self.revealed_at = time.monotonic()
        self.proc = self.server = None

    def prepare_later(self):
        """Start the hidden screen a little into the session."""
        if self.enabled:
            timer = threading.Timer(RETURNING_PREPARE_DELAY, self.prepare)
            timer.daemon = True
            timer.start()

    def prepare(self):
        qml = shutil.which("qml6") or shutil.which("qml")
        with self.lock:
            if self.shown or self.proc or not qml:
                return
            try:
                self.server = self.serve()
                url = f"http://127.0.0.1:{self.server.server_address[1]}/"
                path = os.path.join(STATE, "returning.qml")
                write_file(
                    path,
                    RETURNING_QML
                    % {
                        "title": json.dumps(RETURNING_TITLE),
                        "message": json.dumps(self.MESSAGE),
                        "url": json.dumps(url),
                    },
                )
                env = dict(os.environ, QT_WAYLAND_SHELL_INTEGRATION="layer-shell")
                self.proc = subprocess.Popen([qml, path], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                log(f"returning screen failed to start: {e}")

    def serve(self):
        screen = self
        reveal = self.reveal

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/ready":
                    log("returning screen ready")
                    body = b"ok"
                elif self.path == "/raised":
                    log(f"returning screen raised {time.monotonic() - screen.revealed_at:.2f}s after the quit")
                    body = b"ok"
                else:
                    # Answered at once when it's time to show, otherwise asked again.
                    body = b"show" if reveal.wait(timeout=OSD_POLL_TIMEOUT) else b"wait"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def show(self):
        with self.lock:
            if not self.enabled or self.shown:
                return
            self.shown = True
            ready = self.proc is not None and self.proc.poll() is None
        if ready:
            self.revealed_at = time.monotonic()
            self.reveal.set()
        else:
            # Not started yet (a quit in the first seconds): start one shown.
            show_loading_screen(self.MESSAGE, still=True, overlay=True)


def unload_session_script():
    dbus("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", f"string:{KWIN_SCRIPT_NAME}")


def steam_game_running(appid):
    """Steam wraps every launch (games and shortcuts) in `reaper SteamLaunch AppId=N`."""
    needle = f"AppId={appid}".encode()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        if b"SteamLaunch" in args and needle in args:
            return True
    return False


def watch_stream(proc, hold, stream, started, on_drop=None, question_wait=None, on_quit=None):
    """Follow Moonlight's output during a `stream` launch: pass it through to
    the journal, and keep the loading screen up until the first video arrives.

    `on_drop(code, video)` hears about a connection that ended with Moonlight's
    termination code (None: a reconnect attempt that failed to connect) and
    whether video had started; it returns True if it reconnects, and the
    loading screen then stays for the next attempt."""
    host = stream.get("host", "the host")
    hold.status("stream", f"Connecting to {host}…")
    timer = threading.Timer(STREAM_WAIT, hold.release, ["stream"])
    timer.daemon = True
    timer.start()
    question = None
    video = dropping = False
    step = None

    def show_question():
        log("Moonlight is waiting on a question (another app running on the host?), showing it")
        hold.release("stream")

    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        line = line.rstrip("\n")
        if STREAM_HOST_FOUND.search(line):
            hold.status("stream", "Loading app list…")
            if question is None:
                wait = STREAM_QUESTION_WAIT if question_wait is None else question_wait
                question = threading.Timer(wait, show_question)
                question.daemon = True
                question.start()
        elif request := STREAM_REQUEST.search(line):
            if question:
                question.cancel()
            if request.group(1) == "cancel":
                hold.status("stream", "Quitting the running app…")
            else:
                # The host starts the app (or picks it up) before answering.
                hold.status("stream", f"Waiting for {host}…")
        elif m := STREAM_STEP.search(line):
            step = m.group(1)
            hold.status("stream", f"Starting {STREAM_STEP_NAMES.get(step, step)}…")
        elif step == STREAM_LAST_STEP and line.endswith("done"):
            step = None
            hold.status("stream", "Waiting for video…")
        elif STREAM_STARTED.search(line):
            video = True
        elif STREAM_QUIT.search(line):
            # Moonlight closes its windows next; cover them first.
            log("stream quit")
            if on_quit:
                on_quit()
        elif STREAM_FIRST_VIDEO in line:
            log(f"stream started {time.monotonic() - started:.1f}s after launch")
            hold.release("stream")
        elif terminated := STREAM_TERMINATED.search(line):
            dropping = dropping or bool(on_drop and on_drop(int(terminated.group(1)), video))
        elif STREAM_FAILED.search(line):
            if dropping or (on_drop and not video and on_drop(None, video)):
                dropping = True
                continue
            # Moonlight shows its own error; let it be seen.
            log(f"stream failed: {line.strip()}")
            hold.release("stream")
    timer.cancel()
    if question:
        question.cancel()
    if not dropping:
        hold.release("stream")


class StreamReconnect:
    """Starts a dropped stream again instead of ending the session: after the
    Deck wakes from sleep, or when the host restarts its app. Moonlight would
    show "Connection terminated" and exit once that's dismissed; instead a
    loading screen covers it, Moonlight is closed, and the stream is started
    again once the network is back. Retries for RECONNECT_WINDOW, then
    Moonlight's own error is left on screen."""

    def __init__(self, stream):
        self.host = stream.get("host") or "the host"
        self.app = (stream.get("app") or "").strip() or "the stream"
        self.proc = None
        self.hold = None
        self.deadline = None  # while reconnecting: when to give up
        self.dropped = False

    def on_drop(self, code, video):
        if code in STREAM_NO_RECONNECT or EXIT_REQUESTED.is_set():
            return False
        if code is None and self.deadline is None:
            return False  # the first connection failing: Moonlight's error stands
        now = time.monotonic()
        if video:
            self.deadline = None  # a drop after a good run gets a fresh window
        if self.deadline is not None and now >= self.deadline:
            log("still can't reconnect, leaving Moonlight's error up")
            return False
        self.deadline = self.deadline or now + RECONNECT_WINDOW
        self.dropped = True
        log(f"stream dropped (code {code}), reconnecting" if code is not None else "reconnect failed, trying again")
        # Cover Moonlight's error before closing it (a drop before video still
        # has its loading screen up).
        if self.hold is None or "stream" not in self.hold.reasons:
            # Same layout as the launch's: what's happening, then the step.
            title = f"Reconnecting to {self.app}…"
            self.hold = LoadingHold(show_loading_screen(title, overlay=True), ["stream"], title=title)
        threading.Thread(target=close_app, args=(self.proc,), daemon=True).start()
        return True

    def wait(self):
        """Until the network is back, after a short pause. False to give up."""
        if not network_connected():
            self.hold.status("stream", "Waiting for the network…")
        if EXIT_REQUESTED.wait(RECONNECT_DELAY):
            return False
        while not network_connected():
            if EXIT_REQUESTED.wait(1):
                return False
            if time.monotonic() >= self.deadline:
                log("network didn't come back, giving up on the stream")
                return False
        return True


# Moonlight raises these threads with SDL_SetThreadPriority, which inside
# Flatpak asks the portal, and SteamOS's portal can't map the sandbox's thread
# IDs ("Could not get pidns ... PIDFD_GET_PID_NAMESPACE"), so it never takes,
# in Gaming Mode either. The launcher asks rtkit directly instead, at the nice
# levels SDL asks for (high -10, time-critical -20, which rtkit caps at -15).
MOONLIGHT_PRIORITY_THREADS = {"PacerRender": -10, "AudioDec": -10, "PacerVsync": -15, "PacerVRR": -15}
# SDL's own audio playback thread ("SDLAudioP" + device number), which SDL
# asks to make time-critical the same way.
SDL_AUDIO_THREAD = ("SDLAudioP", -15)
PRIORITY_POLL = 2


def rtkit_high_priority(pid, tid, nice):
    return succeeded(
        run(
            [
                "dbus-send", "--system", "--print-reply", "--dest=org.freedesktop.RealtimeKit1",
                "/org/freedesktop/RealtimeKit1", "org.freedesktop.RealtimeKit1.MakeThreadHighPriorityWithPID",
                f"uint64:{pid}", f"uint64:{tid}", f"int32:{nice}",
            ]
        )
    )  # fmt: skip


def process_group_member(group, comm):
    """A process in `group` named `comm`, or None."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
        except OSError:
            continue
        # comm is in parentheses and may hold spaces; pgrp is the 3rd field after it.
        name, rest = stat[stat.find("(") + 1 : stat.rfind(")")], stat[stat.rfind(")") + 2 :].split()
        if name == comm and len(rest) > 2 and rest[2] == str(group):
            return int(pid)
    return None


def thread_names(pid):
    names = {}
    try:
        tids = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return names
    for tid in tids:
        try:
            with open(f"/proc/{pid}/task/{tid}/comm") as f:
                names[int(tid)] = f.read().strip()
        except OSError:
            continue
    return names


class MoonlightPriority(threading.Thread):
    """Gives Moonlight's pacing and audio threads the priority it asks for.
    They come and go with each stream, so it keeps looking while Moonlight runs."""

    def __init__(self, group):
        super().__init__(daemon=True)
        self.group = group
        self.pid = None
        self.done = set()
        self.stopped = threading.Event()

    def run(self):
        while not self.stopped.wait(PRIORITY_POLL):
            self.check()

    def check(self):
        if self.pid is None or not os.path.exists(f"/proc/{self.pid}"):
            pid = process_group_member(self.group, "moonlight")
            if pid != self.pid:
                self.pid, self.done = pid, set()
            if pid is None:
                return
        for tid, name in thread_names(self.pid).items():
            nice = MOONLIGHT_PRIORITY_THREADS.get(name)
            if name.startswith(SDL_AUDIO_THREAD[0]):
                nice = SDL_AUDIO_THREAD[1]
            if nice is None or tid in self.done:
                continue
            self.done.add(tid)
            if rtkit_high_priority(self.pid, tid, nice):
                log(f"Moonlight's {name} thread at nice {nice}")
            else:
                log(f"rtkit refused to raise Moonlight's {name} thread")

    def stop(self):
        self.stopped.set()


def run_direct(pending, background_steam=False, hold=None, on_quit=None):
    cwd = pending.get("cwd")
    if cwd and not os.path.isdir(cwd):
        log(f"start dir {cwd!r} missing, using home")
        cwd = None
    stream = pending.get("stream")
    if stream and hold is None:
        hold = LoadingHold(None, ["stream"])  # still follow the stream, just without a loading screen
    reconnect = StreamReconnect(stream) if stream and pending.get("reconnect_streams", True) else None
    host_app = None
    if stream:
        host = stream.get("host", "the host")
        host_app = (
            wait_for_host_app(
                pending.get("created"),
                on_wait=lambda: hold.status("stream", f"Waiting for {host}…"),
            )
            or {}
        ).get("state")
    first = True
    while True:
        log(f"exec: {pending['command']}")
        started = time.monotonic()
        output = (
            {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True, "errors": "replace"}
            if stream
            else {}
        )
        # Its own process group, so a "…" hold can close the app and everything
        # it started (Flatpak's wrappers included).
        proc = subprocess.Popen(["bash", "-c", pending["command"]], cwd=cwd or HOME, start_new_session=True, **output)
        watcher = None
        if stream:
            if reconnect:
                reconnect.proc, reconnect.hold, reconnect.dropped = proc, hold, False
            # Another app on the host: Moonlight will ask about it straight away.
            watcher = threading.Thread(
                target=watch_stream,
                args=(proc, hold, stream, started, reconnect.on_drop if reconnect else None),
                kwargs={"question_wait": 0 if first and host_app == "busy" else None, "on_quit": on_quit},
                daemon=True,
            )
            watcher.start()
        priority = None
        if is_moonlight(pending) and shutil.which("dbus-send"):
            priority = MoonlightPriority(proc.pid)
            priority.start()
        if background_steam and first:
            # Give the app a head start on CPU and disk before Steam's heavy startup.
            time.sleep(HYBRID_STEAM_DELAY)
            start_background_steam()
        first = False
        while proc.poll() is None:
            if EXIT_REQUESTED.wait(0.5):
                close_app(proc)
                break
        code = proc.wait()
        if priority:
            priority.stop()
        if watcher:
            watcher.join(timeout=5)  # its last lines decide whether to reconnect
        log(f"exited with {code} after {int(time.monotonic() - started)}s")
        if not (reconnect and reconnect.dropped) or EXIT_REQUESTED.is_set():
            break
        hold = reconnect.hold
        if not reconnect.wait():
            break
    if reconnect and reconnect.dropped:
        reconnect.hold.release("stream")  # a reconnect given up on


def close_app(proc):
    """SIGTERM the app's process group, then SIGKILL whatever ignores it."""
    for sig, wait in ((signal.SIGTERM, APP_CLOSE_GRACE), (signal.SIGKILL, 2)):
        try:
            os.killpg(proc.pid, sig)
        except (OSError, AttributeError):
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def steam_command(url=None):
    # The fast-start flags skip the install checksum Steam runs after Gaming
    # Mode's Steam is killed; -silent keeps its window from covering the game.
    wrapper = shutil.which("steam")
    if wrapper and os.path.realpath(wrapper).endswith("steam-jupiter") and os.path.exists(STEAMOS_CLIENT):
        # Bypass SteamOS's wrapper: its -pipewire flag triggers a screen-capture
        # prompt on Wayland, and its other setup already ran in Gaming Mode.
        cmd = [STEAMOS_CLIENT, "-steamdeck", *STEAM_FAST_START_ARGS]
    elif wrapper:
        cmd = [wrapper, *STEAM_FAST_START_ARGS]
    elif url:
        return ["xdg-open", url]
    else:
        return None
    return [*cmd, url] if url else cmd


def start_background_steam():
    cmd = steam_command()
    if cmd is None:
        log("steam not found, can't start it in the background")
        return
    log(f"background: {' '.join(cmd)}")
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def run_via_steam(pending, loading=None):
    url = f"steam://rungameid/{pending['gameid']}"
    cmd = steam_command(url)
    log(f"launch: {' '.join(cmd)}")
    # Don't wait: if Steam isn't running yet, this process becomes Steam.
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    appid = pending["appid"]
    deadline = time.monotonic() + STEAM_APPEAR_TIMEOUT
    while not steam_game_running(appid):
        if time.monotonic() > deadline:
            log(f"AppId={appid} never started")
            return
        # Poll quickly here so the loading screen closes promptly.
        time.sleep(0.5)
    log(f"AppId={appid} running")
    if loading is not None:
        time.sleep(LOADING_LINGER)
        close_loading_screen(loading)

    misses = 0
    while misses < GONE_POLLS:
        misses = 0 if steam_game_running(appid) else misses + 1
        if EXIT_REQUESTED.wait(POLL_INTERVAL):
            # The game goes with the session on the way back.
            log(f"leaving AppId={appid} running for the session to close")
            return
    log(f"AppId={appid} exited")


def steam_running():
    return succeeded(run(["pgrep", "-x", "steam"]))


def request_steam_shutdown():
    """Ask desktop Steam to exit cleanly. Killed at logout, it would leave its
    crash marker behind and Gaming Mode's Steam would re-verify its install and
    run an update check. Returns when the request was sent, or None."""
    if not steam_running():
        return None
    client = STEAMOS_CLIENT if os.path.exists(STEAMOS_CLIENT) else shutil.which("steam")
    if not client:
        return None
    try:
        subprocess.Popen(
            [client, "-shutdown"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
    except OSError as e:
        log(f"could not ask Steam to shut down: {e}")
        return None
    log("asked desktop Steam to shut down")
    return time.monotonic()


def wait_for_steam_exit(requested):
    while time.monotonic() - requested < STEAM_SHUTDOWN_TIMEOUT:
        if not steam_running():
            log(f"desktop Steam exited cleanly after {time.monotonic() - requested:.1f}s")
            return
        time.sleep(0.2)
    log("desktop Steam still running, returning anyway")


def recover():
    """Run by quickscope-recover.service when the launcher died mid-session:
    undo the session's tweaks and leave it the way the launch would have."""
    log("launcher ended unexpectedly; recovering")
    returning = (read_json(UNDO) or {}).get("return_to_gaming", True)
    # As on the normal way back: desktop Steam killed at logout would make
    # Gaming Mode's Steam re-verify its install.
    steam_shutdown = request_steam_shutdown() if returning else None
    restore(reload=False, restart_shell=not returning)
    if steam_shutdown:
        wait_for_steam_exit(steam_shutdown)
    if returning:
        return_to_gaming()
    return 0


def return_to_gaming():
    # The screen goes black between KWin exiting (it clears the display as it
    # shuts down) and Steam's UI drawing in Gamescope, a few seconds later.
    exe = shutil.which("steamos-session-select")
    if exe:
        log("returning to Gaming Mode")
        if succeeded(run([exe, "gamescope"])):
            return
    # Fallback: SteamOS one-shot Plasma sessions return to Gaming Mode on logout.
    log("falling back to Plasma logout")
    dbus("org.kde.Shutdown", "/Shutdown", "org.kde.Shutdown.logout")


def launch():
    started = time.monotonic()
    pending = claim_pending()
    if pending is None:
        return 0
    log(
        f"--- {pending.get('name')} ({pending.get('appid')}), mode={pending.get('mode')}, "
        f"session={os.environ.get('XDG_SESSION_TYPE', '?')}"
    )
    # For recovery, should this launcher die: where the session should end up.
    record_undo("return_to_gaming", bool(pending.get("return_to_gaming")))

    # KDE's power management sets its own brightness as the desktop starts.
    brightness_raw = pending.get("brightness_raw")
    if brightness_raw is not None:
        write_backlight(brightness_raw)

    # Loading screen right away: KWin's Wayland socket exists as soon as its
    # service has started, before KWin answers on D-Bus.
    starting = f"Starting {pending.get('name') or 'game'}…"
    # A stream's is held until video and closed by the launcher: an overlay.
    held_stream = bool(pending.get("stream")) and pending.get("mode") in ("direct", "hybrid")
    loading = show_loading_screen(starting, overlay=held_stream)

    if wait_for_kwin():
        log(f"KWin ready after {time.monotonic() - started:.1f}s")
    else:
        log("KWin never appeared on D-Bus, launching anyway")
    if pending.get("display"):
        backup_output_config()
        apply_display(pending["display"])
    if loading and loading.poll() is not None:
        log("loading screen exited early, starting it again")
        loading = show_loading_screen(starting, overlay=held_stream)

    # Network, before desktop Steam starts (and restarts NetworkManager): the
    # lock then takes effect on that reconnect.
    hybrid = pending.get("mode") == "hybrid"
    starts_steam = pending.get("mode") in ("hybrid", "steam")
    # Only wpa_supplicant scans in the background and only it gets restarted by
    # desktop Steam. iwd picks access points itself (NetworkManager can't pin
    # one through it) and roams only on a weak signal.
    wpa = wifi_backend() == "wpa_supplicant"
    locked = None
    if pending.get("wifi_lock_ap", True):
        if wpa:
            locked = lock_access_point()
        else:
            log("iwd handles roaming itself, not locking the access point")
    expect_restart = starts_steam and wpa
    # Hold the loading screen over the app until the network has settled
    # and, for a direct-to-stream launch, until video is on screen.
    hold_reasons = []
    if expect_restart or locked:
        hold_reasons.append("network")
    if held_stream:
        hold_reasons.append("stream")
    hold = LoadingHold(loading, hold_reasons, title=starting) if loading and hold_reasons else None
    hold_loading = hold is not None

    script = bool(
        (pending.get("force_fullscreen") or loading or hybrid)
        and load_session_script(
            pending.get("force_fullscreen"), loading, minimize_steam_windows=hybrid, hold_loading=hold_loading
        )
    )
    if loading and not script:
        # Without the script nothing can tell when the app's window is up.
        timer = threading.Timer(LOADING_FALLBACK, close_loading_screen, [loading])
        timer.daemon = True
        timer.start()

    chosen = pending.get("power_profile", "auto")
    profile = resolve_power_profile(chosen)
    if chosen == "auto":
        log(f"automatic power profile: {'plugged in' if plugged_in() else 'on battery'}")
    extra = {"wifi_powersave": "disabled"} if pending.get("wifi_powersave_off", True) else {}
    power = apply_power_profile(profile, extra)
    keeper = None
    if power or brightness_raw is not None:
        hold_for = None if pending.get("lock_brightness") else BRIGHTNESS_SETTLE_TIME
        keeper = SessionKeeper(
            power, brightness_raw, hold_for, auto_profile=profile if chosen == "auto" and power else None
        )
        keeper.start()
    # Plasma's panel, which the session skips, normally draws the volume
    # indicator; brightness has no control at all outside Gaming Mode.
    osd = Osd()
    if not osd.start():
        osd = None
    volume = VolumeWatcher(osd) if osd else None
    if volume and not volume.start():
        volume = None
    battery = BatteryWarning(osd) if osd and battery_status() else None
    if battery:
        battery.start()
    combo = BrightnessCombo(osd, keeper, on_tap=battery.show_status if battery else None)
    combo.start()
    returning = pending.get("return_to_gaming")
    returning_screen = ReturningScreen(returning)
    returning_screen.prepare_later()
    if hold and "network" in hold.reasons:
        # Desktop Steam restarts NetworkManager (wpa_supplicant); otherwise
        # reconnect once ourselves so the access point lock applies.
        NetworkGate(hold, expect_restart=expect_restart, reconnect=None if expect_restart else locked).start()
    try:
        log(f"launching {time.monotonic() - started:.1f}s after start")
        if pending.get("mode") in ("direct", "hybrid"):
            run_direct(
                pending, background_steam=pending.get("mode") == "hybrid", hold=hold, on_quit=returning_screen.show
            )
        else:
            # Steam's game process appearing is the fallback signal when the
            # KWin script couldn't load.
            run_via_steam(pending, None if script else loading)
    except Exception as e:
        log(f"launch failed: {e!r}")
    finally:
        close_loading_screen(loading)
        # Covers the clean-up and logout (if the stream's quit didn't already).
        returning_screen.show()
        steam_shutdown = request_steam_shutdown() if returning else None
        combo.stop()
        if battery:
            battery.stop()
        if volume:
            volume.stop()
        if osd:
            osd.stop()
        if keeper:
            keeper.stop()
        if script:
            unload_session_script()
        # Undo before returning: the logout ends this process.
        restore(restart_shell=not returning)
        if steam_shutdown:
            wait_for_steam_exit(steam_shutdown)

    if returning:
        return_to_gaming()
    return 0


# --- diagnostics -------------------------------------------------------------

DIAGNOSTICS_LOG_LINES = 300
MAC_ADDRESS = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
IPV4_ADDRESS = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def redact(text, wifi_names=()):
    """Mask what could identify a person or place before a report is shared:
    MAC addresses (an access point's BSSID can locate a home), IP addresses
    and Wi-Fi network names."""
    text = MAC_ADDRESS.sub("xx:xx:xx:xx:xx:xx", text)
    text = IPV4_ADDRESS.sub("x.x.x.x", text)
    for name in wifi_names:
        if name:
            text = text.replace(name, "<wifi>")
    return text


def os_release():
    values = {}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                values[key] = value.strip('"')
    except OSError:
        pass
    return values


def diagnostics():
    """A shareable report for bug reports: system, display, Moonlight and the
    recent launcher log, with personal details masked."""
    release = os_release()

    def steamosctl(command):
        out = run(["steamosctl", command])
        if not succeeded(out):
            return "?"
        # "Label: value" lines; keep the values.
        return ", ".join(line.split(":", 1)[-1].strip() for line in out.stdout.strip().splitlines())

    wifi = active_wifi()
    lines = [
        f"SteamOS: {release.get('PRETTY_NAME', '?')} (build {release.get('BUILD_ID', '?')})",
        f"Kernel: {platform.release()}",
        f"Device: {steamosctl('get-device-model')}",
        f"Wi-Fi: backend {steamosctl('get-wifi-backend')}, "
        f"power saving {steamosctl('get-wifi-power-management-state')}, "
        f"connected over Wi-Fi: {'yes' if wifi else 'no'}",
        f"CPU scheduler: {get_power_setting('scheduler') or '?'}; governor: {get_power_setting('governor') or '?'}",
    ]
    display = display_info()
    if display:
        display = {k: v for k, v in display.items() if k != "modes"} | {"mode_count": len(display["modes"])}
    lines.append(f"Display: {json.dumps(display)}")
    installed = succeeded(run(["flatpak", "info", MOONLIGHT_FLATPAK]))
    # Fork builds carry no Flatpak version; Moonlight logs its own at startup.
    started = run(["journalctl", "--user", "--no-pager", "-o", "cat", "-g", "Current Moonlight version"])
    versions = re.findall(r'Current Moonlight version: "([^"]+)"', started.stdout) if started else []
    conf = read_ini_section(moonlight_conf()) or {}
    # An AppImage or a native build keeps its settings here.
    native = os.path.exists(os.path.join(HOME, ".config", MOONLIGHT_CONF_NAME))
    lines.append(
        f"Moonlight: Flatpak {'installed' if installed else 'not installed'}, "
        f"native/AppImage settings {'present' if native else 'absent'}, "
        f"last run {versions[-1] if versions else 'unknown'}, "
        f"Nonary's VRR fork: {'yes' if moonlight_fork(conf=conf) else 'no'}"
    )
    try:
        with open(LOG) as f:
            recent = f.readlines()[-DIAGNOSTICS_LOG_LINES:]
    except OSError:
        recent = ["(no launcher log)\n"]
    lines += ["", f"--- last {len(recent)} lines of launcher.log", "".join(recent).rstrip()]
    names = [wifi[1]] if wifi else []
    lock = read_json(WIFI_LOCK)
    if lock:
        names.append(lock.get("connection"))
    return redact("\n".join(lines), names)


def uninstall():
    """Undo everything and remove the files Quickscope created.

    Decky also runs this when installing an update over the plugin, so the log
    and a Wi-Fi unlock that couldn't run yet are kept; the next version picks
    them up. Everything else (this script's copy, generated files) goes."""
    restore(reload=False)
    remove(os.path.join(UNIT_DIR, f"{UNIT_TARGET}.wants", UNIT_NAME))
    remove(os.path.join(UNIT_DIR, UNIT_NAME))
    remove(os.path.join(UNIT_DIR, RECOVER_UNIT_NAME))
    systemctl("daemon-reload")
    keep = {LOG, WIFI_LOCK}
    for path in glob.glob(os.path.join(STATE, "*")):
        if path not in keep:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                remove(path)
    try:
        os.rmdir(STATE)  # only if nothing was kept
    except OSError:
        pass


def main(argv):
    command = argv[1] if len(argv) > 1 else None
    if command == "--prepare":
        return prepare()
    if command == "--restore":
        restore()
        return 0
    if command == "--displays":
        print(json.dumps(display_info()))
        return 0
    if command == "--recover":
        return recover()
    if command == "--diagnostics":
        print(diagnostics())
        return 0
    if command == "--moonlight-hosts":
        print(json.dumps(moonlight_hosts()))
        return 0
    if command == "--start-host-app":
        return start_host_app()
    if command == "--quit-host-app":
        return quit_host_app()
    if command == "--moonlight-settings":
        current = read_ini_section(moonlight_conf()) or {}
        values = {field: current.get(key) for field, key in MOONLIGHT_KEYS.items()}
        values["vrr_fork"] = moonlight_fork(conf=current)
        values["auto_bitrate"] = current.get("autoadjustbitrate")
        print(json.dumps(values))
        return 0
    if command == "--uninstall":
        uninstall()
        return 0
    return launch()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
