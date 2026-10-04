"""Tests for defaults/quickscope_launcher.py, run against a temporary HOME."""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_launcher(home, config_dirs):
    """Import a fresh copy of the launcher; its paths are fixed at import."""
    os.environ["HOME"] = os.environ["USERPROFILE"] = home
    os.environ["XDG_CONFIG_DIRS"] = config_dirs
    spec = importlib.util.spec_from_file_location("quickscope_launcher", ROOT / "defaults" / "quickscope_launcher.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    launcher.systemctl = lambda *args: None  # never touch the real systemd
    return launcher


class LauncherTestCase(unittest.TestCase):
    def setUp(self):
        self.saved_env = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "XDG_CONFIG_DIRS")}
        self.home = tempfile.mkdtemp()
        self.sysconf = tempfile.mkdtemp()
        self.l = load_launcher(self.home, self.sysconf)

    def tearDown(self):
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.sysconf, ignore_errors=True)


class QuietAutostart(LauncherTestCase):
    def write_entry(self, directory, name, exe):
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, name), "w") as f:
            f.write(f"[Desktop Entry]\nExec={exe}\n")

    def test_hides_steam_and_kde_helpers_but_never_user_apps(self):
        system = os.path.join(self.sysconf, "autostart")
        for name, exe in [
            ("baloo_file.desktop", "baloo_file"),
            ("steam.desktop", "/usr/bin/steam -silent %U"),
            ("org.kde.kdeconnect.daemon.desktop", "kdeconnectd"),
            ("polkit-kde-authentication-agent-1.desktop", "polkit"),
        ]:
            self.write_entry(system, name, exe)
        user_app = "com.github.zocker_160.SyncThingy.desktop"
        self.write_entry(self.l.AUTOSTART_DIR, user_app, "flatpak run x")

        hidden = self.l.hide_autostart(
            lambda name, entry, is_user: (
                self.l.is_steam_entry(entry) or (not is_user and name in self.l.QUIET_AUTOSTART)
            )
        )

        self.assertEqual(
            sorted(os.path.basename(h["path"]) for h in hidden),
            ["baloo_file.desktop", "org.kde.kdeconnect.daemon.desktop", "steam.desktop"],
        )
        with open(os.path.join(self.l.AUTOSTART_DIR, user_app)) as f:
            self.assertIn("Exec=flatpak", f.read())
        self.l.restore(reload=False)
        self.assertEqual(os.listdir(self.l.AUTOSTART_DIR), [user_app])


class Splash(LauncherTestCase):
    def fake_kconfig(self, stored):
        """Stub run() as kreadconfig6/kwriteconfig6 over a dict; return the call log."""
        calls = []

        class Result:
            def __init__(self, out=""):
                self.returncode, self.stdout = 0, out

        def run(cmd):
            calls.append(cmd)
            key = cmd[cmd.index("--key") + 1]
            if cmd[0] == "kreadconfig6":
                return Result(stored.get(key, "") + "\n")
            if "--delete" in cmd:
                stored.pop(key, None)
            else:
                stored[key] = cmd[-1]
            return Result()

        self.l.run = run
        self.l.kconfig_tool = lambda kind: f"k{kind}config6"
        return calls

    def write_ksplashrc(self):
        self.l.write_file(self.l.KSPLASHRC, "[KSplash]\n")

    def test_session_created_file_is_removed(self):
        stored = {}
        self.fake_kconfig(stored)
        self.l.disable_splash()
        self.assertEqual(stored, {"Engine": "none"})
        self.write_ksplashrc()  # what kwriteconfig6 would have created
        self.l.restore(reload=False)
        self.assertFalse(os.path.exists(self.l.KSPLASHRC))

    def test_unset_engine_in_existing_file_is_deleted_again(self):
        self.write_ksplashrc()
        stored = {}
        self.fake_kconfig(stored)
        self.l.disable_splash()
        self.l.restore(reload=False)
        self.assertEqual(stored, {})
        self.assertTrue(os.path.exists(self.l.KSPLASHRC))

    def test_existing_engine_is_restored(self):
        self.write_ksplashrc()
        stored = {"Engine": "KSplashQML"}
        self.fake_kconfig(stored)
        self.l.disable_splash()
        self.assertEqual(stored["Engine"], "none")
        self.l.restore(reload=False)
        self.assertEqual(stored, {"Engine": "KSplashQML"})

    def test_already_off_is_left_alone(self):
        stored = {"Engine": "none"}
        calls = self.fake_kconfig(stored)
        self.l.disable_splash()
        self.l.restore(reload=False)
        self.assertEqual(stored, {"Engine": "none"})
        self.assertFalse(any(c[0] == "kwriteconfig6" for c in calls))


class SessionScript(LauncherTestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_templated_kwin_script_is_valid_js(self):
        js = (
            self.l.SESSION_JS.replace("%LOADING_TITLE%", self.l.LOADING_TITLE)
            .replace("%OSD_TITLE%", self.l.OSD_TITLE)
            .replace("%LOADING_PID%", "1234")
            .replace("%FORCE_FULLSCREEN%", "true")
            .replace("%MINIMIZE_STEAM_WINDOWS%", "true")
            .replace("%HOLD_LOADING%", "false")
        )
        self.assertNotIn("%", js)
        path = os.path.join(self.home, "session.js")
        with open(path, "w") as f:
            f.write(js)
        result = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_hybrid_minimizes_steam_windows_but_not_the_app(self):
        js = (
            self.l.SESSION_JS.replace("%LOADING_TITLE%", self.l.LOADING_TITLE)
            .replace("%OSD_TITLE%", self.l.OSD_TITLE)
            .replace("%LOADING_PID%", "-1")
            .replace("%FORCE_FULLSCREEN%", "true")
            .replace("%MINIMIZE_STEAM_WINDOWS%", "true")
            .replace("%HOLD_LOADING%", "false")
        )
        # A fake KWin: windows arrive in order, as when Steam's button opens it mid-stream.
        harness = """
function signal() {
  const handlers = [];
  return { handlers, connect: (f) => handlers.push(f), disconnect: (f) => handlers.splice(handlers.indexOf(f), 1),
           emit: (...a) => [...handlers].forEach((f) => f(...a)) };
}
const workspace = { windowAdded: signal(), windowRemoved: signal(), windowActivated: signal(),
                    windowList: () => [], activeWindow: null };
function win(cls) {
  return { resourceClass: cls, caption: cls, normalWindow: true, pid: 1, closed: false,
           minimized: false, fullScreen: false, minimizedChanged: signal(),
           closeWindow() { this.closed = true; } };
}
%SCRIPT%
const steamUpdate = win("steam");
const app = win("com.moonlight_stream.Moonlight");
const steamStore = win("steam");
for (const w of [steamUpdate, app, steamStore]) workspace.windowAdded.emit(w);
// The Steam button: Steam un-minimizes and activates its window.
steamStore.minimized = false;
steamStore.minimizedChanged.emit();
workspace.activeWindow = steamStore;
workspace.windowActivated.emit(steamStore);
const state = (w) => ({ closed: w.closed, minimized: w.minimized, fullScreen: w.fullScreen });
console.log(JSON.stringify({ windows: [steamUpdate, app, steamStore].map(state),
                             appActive: workspace.activeWindow === app }));
""".replace("%SCRIPT%", js)
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        steam_update, app, steam_store = out["windows"]
        # Closing a Steam window quits Steam (no tray) or cancels its start: never.
        self.assertEqual(steam_update, {"closed": False, "minimized": True, "fullScreen": False})
        self.assertEqual(steam_store, {"closed": False, "minimized": True, "fullScreen": False})
        self.assertEqual(app, {"closed": False, "minimized": False, "fullScreen": True})
        self.assertTrue(out["appActive"])

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_held_loading_screen_stays_in_front_until_closed(self):
        js = (
            self.l.SESSION_JS.replace("%LOADING_TITLE%", self.l.LOADING_TITLE)
            .replace("%OSD_TITLE%", self.l.OSD_TITLE)
            .replace("%LOADING_PID%", "42")
            .replace("%FORCE_FULLSCREEN%", "true")
            .replace("%MINIMIZE_STEAM_WINDOWS%", "true")
            .replace("%HOLD_LOADING%", "true")
        )
        harness = """
function signal() {
  const handlers = [];
  return { connect: (f) => handlers.push(f), disconnect: (f) => handlers.splice(handlers.indexOf(f), 1),
           emit: (...a) => [...handlers].forEach((f) => f(...a)) };
}
let open = [];
const workspace = { windowAdded: signal(), windowRemoved: signal(), windowActivated: signal(),
                    windowList: () => open, activeWindow: null };
function win(cls, pid) {
  return { resourceClass: cls, caption: cls, normalWindow: true, pid, closed: false, minimized: false,
           fullScreen: false, minimizedChanged: signal(), closeWindow() { this.closed = true; } };
}
%SCRIPT%
const loading = win("qml6", 42);
const app = win("com.moonlight_stream.Moonlight", 7);
open = [loading];
workspace.windowAdded.emit(loading);
open = [loading, app];
workspace.windowAdded.emit(app);
const whileHeld = { active: workspace.activeWindow === loading ? "loading" : "app", closed: loading.closed,
                    fullScreen: app.fullScreen };
// The launcher closes the loading screen once the network is ready.
open = [app];
workspace.windowRemoved.emit(loading);
console.log(JSON.stringify({ whileHeld, after: workspace.activeWindow === app ? "app" : "other" }));
""".replace("%SCRIPT%", js)
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["whileHeld"], {"active": "loading", "closed": False, "fullScreen": True})
        self.assertEqual(out["after"], "app")


class Brightness(LauncherTestCase):
    def setUp(self):
        super().setUp()
        self.l.BACKLIGHT_ROOT = os.path.join(self.home, "backlight")
        self.dev = os.path.join(self.l.BACKLIGHT_ROOT, "amdgpu_bl0")
        self.l.write_file(os.path.join(self.dev, "max_brightness"), "65535\n")
        self.set_raw(17011)

    def set_raw(self, raw):
        self.l.write_file(os.path.join(self.dev, "brightness"), f"{raw}\n")

    def raw(self):
        return self.l.read_backlight()[0]

    def test_curve_matches_steam_measurements(self):
        # Measured on a Deck LCD: Steam 100% -> 65535, Steam 48% -> 17011.
        self.assertEqual(self.l.brightness_pct_to_raw(100, 65535), 65535)
        self.assertAlmostEqual(self.l.brightness_pct_to_raw(48, 65535), 17011, delta=400)
        self.assertGreaterEqual(self.l.brightness_pct_to_raw(0, 65535), 1)

    def test_match_gaming_mode_locks_current_level_and_restores(self):
        pending = {"lock_brightness": True, "match_gaming_brightness": True}
        self.l.choose_brightness(pending)
        self.assertEqual(pending["brightness_raw"], 17011)
        self.set_raw(40000)  # KDE changed it during the session
        self.l.restore(reload=False)
        self.assertEqual(self.raw(), 17011)

    def test_custom_level(self):
        pending = {"lock_brightness": True, "match_gaming_brightness": False, "brightness_pct": 100}
        self.l.choose_brightness(pending)
        self.assertEqual(pending["brightness_raw"], 65535)

    def test_no_backlight_is_harmless(self):
        self.l.BACKLIGHT_ROOT = os.path.join(self.home, "nothing")
        pending = {"lock_brightness": True}
        self.l.choose_brightness(pending)
        self.assertNotIn("brightness_raw", pending)
        self.assertFalse(self.l.write_backlight(100))

    def test_raw_to_pct_inverts_the_curve(self):
        for pct in (5, 48, 50, 100):
            self.assertEqual(self.l.brightness_raw_to_pct(self.l.brightness_pct_to_raw(pct, 65535), 65535), pct)

    def test_steps_snap_to_grid_and_clamp(self):
        step = self.l.step_brightness_pct
        self.assertEqual(step(48, 1), 50)
        self.assertEqual(step(48, -1), 45)
        self.assertEqual(step(50, 1), 55)
        self.assertEqual(step(50, -1), 45)
        self.assertEqual(step(100, 1), 100)
        self.assertEqual(step(5, -1), 5)

    def test_unlocked_hold_releases(self):
        keeper = self.l.SessionKeeper({}, 17011, hold_brightness_for=0)
        self.l.KEEPER_INTERVAL = 0.01
        keeper.start()
        time.sleep(0.1)
        keeper.stop()
        self.assertIsNone(keeper.brightness_raw)

    def test_combo_step_moves_lock_target(self):
        keeper = self.l.SessionKeeper({}, 17011)
        combo = self.l.BrightnessCombo(None, keeper)
        combo.step(1)
        self.assertEqual(self.raw(), self.l.brightness_pct_to_raw(50, 65535))
        self.assertEqual(keeper.brightness_raw, self.raw())


class Power(LauncherTestCase):
    def setUp(self):
        super().setUp()
        # A fake steamosctl holding SteamOS's defaults.
        self.state = {"gpu": "auto", "governor": "powersave", "boost": "enabled", "scheduler": "lavd"}
        commands = {cmd: (key, i) for key, cmds in self.l.POWER_SETTINGS.items() for i, cmd in enumerate(cmds)}

        def fake_run(cmd):
            key, is_set = commands[cmd[1]]
            if is_set:
                self.state[key] = cmd[2]
            return subprocess.CompletedProcess(cmd, 0, f"Something: {self.state[key]}\n")

        self.l.run = fake_run
        # Read through the fake too, not the test machine's real sysfs.
        self.l.SYSFS_ROOT = os.path.join(self.home, "no-sysfs")
        real_which = shutil.which
        shutil.which = lambda name: "/usr/bin/" + name
        self.addCleanup(setattr, shutil, "which", real_which)

    def test_sysfs_is_read_in_steamosctl_terms(self):
        root = self.l.SYSFS_ROOT
        self.l.write_file(os.path.join(root, "class/drm/card0/device/power_dpm_force_performance_level"), "high\n")
        self.l.write_file(os.path.join(root, "devices/system/cpu/cpu0/cpufreq/scaling_governor"), "powersave\n")
        self.l.write_file(os.path.join(root, "devices/system/cpu/cpufreq/boost"), "0\n")
        self.l.write_file(os.path.join(root, "kernel/sched_ext/state"), "enabled\n")
        self.l.write_file(os.path.join(root, "kernel/sched_ext/root/ops"), "lavd_1.1.3_x86_64_unknown_linux_gnu\n")
        keys = ("gpu", "governor", "boost", "scheduler")
        self.assertEqual([self.l.get_power_setting(k) for k in keys], ["high", "powersave", "disabled", "lavd"])
        self.l.write_file(os.path.join(root, "kernel/sched_ext/state"), "disabled\n")
        self.assertEqual(self.l.get_power_setting("scheduler"), "none")

    def test_battery_profile_applies_and_restores(self):
        applied = self.l.apply_power_profile("battery")
        self.assertEqual(applied, {"gpu": "auto", "governor": "powersave", "boost": "disabled", "scheduler": "none"})
        self.assertEqual(self.state["boost"], "disabled")
        self.l.restore(reload=False)
        self.assertEqual(self.state, {"gpu": "auto", "governor": "powersave", "boost": "enabled", "scheduler": "lavd"})

    def test_performance_profile(self):
        self.l.apply_power_profile("performance")
        self.assertEqual(
            self.state, {"gpu": "high", "governor": "performance", "boost": "enabled", "scheduler": "none"}
        )

    def test_unknown_profile_does_nothing(self):
        self.assertEqual(self.l.apply_power_profile("turbo"), {})

    def supplies(self, **supplies):
        self.l.POWER_SUPPLY_ROOT = os.path.join(self.home, "power_supply")
        for name, (kind, online) in supplies.items():
            self.l.write_file(os.path.join(self.l.POWER_SUPPLY_ROOT, name, "type"), kind + "\n")
            if online is not None:
                self.l.write_file(os.path.join(self.l.POWER_SUPPLY_ROOT, name, "online"), online + "\n")

    def test_auto_follows_the_charger(self):
        self.supplies(ACAD=("Mains", "1"), BAT1=("Battery", None))
        self.assertEqual(self.l.resolve_power_profile("auto"), "performance")
        self.supplies(ACAD=("Mains", "0"), BAT1=("Battery", None))
        self.assertEqual(self.l.resolve_power_profile("auto"), "battery")
        self.assertEqual(self.l.resolve_power_profile("battery"), "battery")

    def test_auto_without_a_battery_is_performance(self):
        self.supplies()
        self.assertEqual(self.l.resolve_power_profile("auto"), "performance")

    def test_keeper_switches_when_unplugged(self):
        self.supplies(ACAD=("Mains", "1"), BAT1=("Battery", None))
        power = self.l.apply_power_profile("performance")
        keeper = self.l.SessionKeeper(power, None, auto_profile="performance")
        self.l.KEEPER_INTERVAL = 0.01
        self.supplies(ACAD=("Mains", "0"), BAT1=("Battery", None))
        keeper.start()
        time.sleep(0.2)
        keeper.stop()
        self.assertEqual(self.state["boost"], "disabled")


class Network(LauncherTestCase):
    """Fake nmcli: one Wi-Fi connection whose saved BSSID the tests can see."""

    def setUp(self):
        super().setUp()
        self.profile_bssid = ""
        self.fail_modify = False

        def fake_run(cmd):
            if cmd[:2] == ["nmcli", "-t"] and cmd[-1] == "device":
                out = (
                    "eth0:ethernet:unavailable:\nwlan0:wifi:connected:My\\:Net\nlo:loopback:connected (externally):lo\n"
                )
            elif "wifi" in cmd and "list" in cmd:
                out = "no:BC\\:51\\:5F\\:57\\:11\\:6C\nyes:BC\\:51\\:5F\\:57\\:11\\:68\n"
            elif cmd[:3] == ["nmcli", "-g", "802-11-wireless.bssid"]:
                out = self.profile_bssid.replace(":", "\\:") + "\n"
            elif cmd[:3] == ["nmcli", "connection", "modify"]:
                if self.fail_modify:
                    return subprocess.CompletedProcess(cmd, 1, "Error: insufficient privileges\n")
                self.assertEqual(cmd[3], "My:Net")
                self.profile_bssid = cmd[5]
                out = ""
            else:
                out = ""
            return subprocess.CompletedProcess(cmd, 0, out)

        self.l.run = fake_run

    def test_active_wifi_unescapes_nmcli(self):
        self.assertEqual(self.l.active_wifi(), ("wlan0", "My:Net", "BC:51:5F:57:11:68"))

    def test_lock_and_unlock(self):
        self.assertEqual(self.l.lock_access_point(), "My:Net")
        self.assertEqual(self.profile_bssid, "BC:51:5F:57:11:68")
        self.l.restore(reload=False)
        self.assertEqual(self.profile_bssid, "")
        self.assertFalse(os.path.exists(self.l.WIFI_LOCK))

    def test_the_users_own_lock_is_left_alone(self):
        self.profile_bssid = "AA:BB:CC:DD:EE:FF"
        self.assertIsNone(self.l.lock_access_point())
        self.assertEqual(self.profile_bssid, "AA:BB:CC:DD:EE:FF")

    def test_failed_unlock_is_retried_later(self):
        self.l.lock_access_point()
        self.fail_modify = True
        self.l.restore(reload=False)  # e.g. from outside the desktop session
        self.assertTrue(os.path.exists(self.l.WIFI_LOCK))
        self.fail_modify = False
        self.l.lock_access_point()  # next session: unlocks first, then locks again
        self.l.restore(reload=False)
        self.assertEqual(self.profile_bssid, "")
        self.assertFalse(os.path.exists(self.l.WIFI_LOCK))


# EDID of a Samsung 4K HDR TV, as `modetest -c` prints it.
TV_EDID = [
    "00ffffffffffff004c2db571000e0001",
    "011f0103806639780aa833ab5045a527",
    "0d4848bdef80714f81c0810081809500",
    "a9c0b300d1c0e2d1008cf0705a806808",
    "8a00501d7400001e565e00a0a0a02950",
    "30203500501d7400001a000000fd0018",
    "4b0f873c000a202020202020000000fc",
    "0053414d53554e470a2020202020015e",
    "02035cf05661661f041313132021225d",
    "5e5f6065666264646403122f09070709",
    "070709070709070709070783010000e2",
    "004fe305c3016e030c001000903c2800",
    "800102030468d85dc40178800900e306",
    "0d01e30f0300e5018b849001023a8018",
    "71382d40582c450000000000001e0000",
    "00000000000000000000000000000037",
]

# Trimmed `modetest -c` / `-p` output from a docked Deck LCD on a 4K TV, with
# Gamescope outputting HDR.
MODETEST_CONNECTORS = (
    """\
Connectors:
id\tencoder\tstatus\t\tname\t\tsize (mm)\tmodes\tencoders
135\t0\tconnected\teDP-1          \t100x150\t\t2\t134
  modes:
\tindex name refresh (Hz) hdisp hss hse htot vdisp vss vse vtot
  #0 800x1280 60.00 800 832 852 872 1280 1296 1298 1324 69270 flags: phsync, pvsync; type: preferred, driver
  #1 256x160 58.79 256 264 288 320 160 163 169 172 3236 flags: nhsync, pvsync; type:
  props:
\t1 EDID:
145\t144\tconnected\tDP-1           \t1020x570\t\t51\t144
  modes:
\tindex name refresh (Hz) hdisp hss hse htot vdisp vss vse vtot
  #0 3840x2160 60.00 3840 3944 3952 3980 2160 2168 2178 2250 537300 flags: phsync, pvsync; type: preferred, driver
  #1 3840x2160 60.00 3840 4016 4104 4400 2160 2168 2178 2250 594000 flags: phsync, pvsync; type: driver
  #2 1920x1080 59.94 1920 2008 2052 2200 1080 1084 1089 1125 148352 flags: phsync, pvsync; type: driver
  props:
\t1 EDID:
\t\tflags: immutable blob
\t\tblobs:

\t\tvalue:
"""
    + "".join(f"\t\t\t{row}\n" for row in TV_EDID)
    + """\
\t148 Colorspace:
\t\tflags: enum
\t\tenums: Default=0 BT709_YCC=2 opRGB=7 BT2020_RGB=9 BT2020_YCC=10
\t\tvalue: 9
150\t0\tdisconnected\tHDMI-A-1       \t0x0\t\t0\t149
"""
)
MODETEST_CRTCS = """\
CRTCs:
id\tfb\tpos\tsize
118\t165\t(0,0)\t(3840x2160)
  #0 3840x2160 60.00 3840 3944 3952 3980 2160 2168 2178 2250 537300 flags: phsync, pvsync; type: preferred, driver
  props:
123\t0\t(0,0)\t(0x0)
  #0  -nan 0 0 0 0 0 0 0 0 0 flags: ; type:
Planes:
"""


class Display(LauncherTestCase):
    def test_parse_connectors(self):
        connectors = self.l.parse_modetest_connectors(MODETEST_CONNECTORS)
        self.assertEqual(connectors["eDP-1"]["modes"], ["800x1280@60.00"])
        self.assertEqual(connectors["DP-1"]["modes"], ["3840x2160@60.00", "1920x1080@59.94"])
        self.assertFalse(connectors["HDMI-A-1"]["connected"])
        self.assertTrue(connectors["DP-1"]["hdr_capable"])
        self.assertTrue(connectors["DP-1"]["hdr"])
        self.assertFalse(connectors["eDP-1"]["hdr_capable"])

    def test_edid_without_hdr(self):
        edid = bytes.fromhex("".join(TV_EDID))
        # Drop the HDR static metadata block's PQ bit.
        no_pq = edid.replace(bytes.fromhex("e3060d01"), bytes.fromhex("e3060901"))
        self.assertTrue(self.l.edid_supports_hdr(edid))
        self.assertFalse(self.l.edid_supports_hdr(no_pq))
        self.assertFalse(self.l.edid_supports_hdr(edid[:128]))

    def test_active_mode(self):
        self.assertEqual(self.l.parse_modetest_active_modes(MODETEST_CRTCS), ["3840x2160@60.00"])

    def fake_modetest(self, connectors=MODETEST_CONNECTORS, crtcs=MODETEST_CRTCS):
        def fake_run(cmd):
            out = connectors if cmd[-1] == "-c" else crtcs
            return subprocess.CompletedProcess(cmd, 0, out)

        self.l.run = fake_run
        real_which = shutil.which
        shutil.which = lambda name: "/usr/bin/" + name
        self.addCleanup(setattr, shutil, "which", real_which)

    def test_docked_matches_gaming_mode(self):
        self.fake_modetest()
        info = self.l.display_info()
        self.assertEqual((info["connector"], info["external"], info["current"]), ("DP-1", True, "3840x2160@60.00"))
        pending = {}
        self.l.choose_display(pending, self.l.display_info())
        self.assertEqual(
            pending["display"],
            {"connector": "DP-1", "mode": "3840x2160@60.00", "hdr": True, "scale": 1.0, "disable": ["eDP-1"]},
        )

    def test_forced_mode_and_fallback(self):
        self.fake_modetest()
        pending = {"display_mode": "1920x1080@59.94"}
        self.l.choose_display(pending, self.l.display_info())
        self.assertEqual((pending["display"]["mode"], pending["display"]["hdr"]), ("1920x1080@59.94", False))
        pending = {"display_mode": "1920x1080@59.94", "display_hdr": True}
        self.l.choose_display(pending, self.l.display_info())
        self.assertTrue(pending["display"]["hdr"])
        pending = {"display_mode": "2560x1440@144.00"}
        self.l.choose_display(pending, self.l.display_info())
        self.assertEqual(pending["display"]["mode"], "3840x2160@60.00")

    def test_handheld_lcd_only_gets_a_scale(self):
        lcd_only = MODETEST_CONNECTORS.split("145\t144")[0]
        self.fake_modetest(lcd_only, MODETEST_CRTCS.replace("3840x2160 60.00", "800x1280 60.00"))
        pending = {"display_scale": 150}
        self.l.choose_display(pending, self.l.display_info())
        self.assertEqual(
            pending["display"], {"connector": "eDP-1", "mode": None, "hdr": None, "scale": 1.5, "disable": []}
        )

    def test_closest_kscreen_mode(self):
        ids = {"3840x2160@60.00": "1", "3840x2160@59.94": "2", "1920x1080@60.00": "3"}
        self.assertEqual(self.l.closest_mode_id(ids, "3840x2160@59.94"), "2")
        self.assertEqual(self.l.closest_mode_id({"800x1280@60.00": "1"}, "800x1280@59.99"), "1")
        self.assertIsNone(self.l.closest_mode_id(ids, "2560x1440@60.00"))

    def test_only_differences_are_applied(self):
        outputs = {
            "eDP-1": {"enabled": False, "mode": "1", "modes": {"800x1280@60.00": "1"}},
            "DP-1": {"enabled": True, "mode": "9", "modes": {"3840x2160@60.00": "9", "1920x1080@60.00": "18"}},
        }
        display = {"connector": "DP-1", "mode": "3840x2160@60.00", "disable": ["eDP-1"]}
        self.assertEqual(self.l.display_changes(outputs, display), [])
        outputs["DP-1"]["hdr"] = False
        self.assertEqual(
            self.l.display_changes(outputs, dict(display, hdr=True)),
            ["output.DP-1.hdr.enable", "output.DP-1.wcg.enable"],
        )
        self.assertEqual(self.l.display_changes(outputs, dict(display, hdr=False)), [])
        outputs["DP-1"]["scale"] = 1.7
        self.assertEqual(self.l.display_changes(outputs, dict(display, scale=1.0)), ["output.DP-1.scale.1"])
        self.assertEqual(self.l.display_changes(outputs, dict(display, scale=1.7)), [])
        display["mode"] = "1920x1080@60.00"
        self.assertEqual(self.l.display_changes(outputs, display), ["output.DP-1.mode.18"])
        outputs["eDP-1"]["enabled"] = True
        self.assertEqual(self.l.display_changes(outputs, display), ["output.eDP-1.disable", "output.DP-1.mode.18"])

    def test_output_config_restored(self):
        self.l.write_file(self.l.KWIN_OUTPUT_CONFIG, "original")
        self.l.backup_output_config()
        self.l.write_file(self.l.KWIN_OUTPUT_CONFIG, "changed by the session")
        self.l.restore(reload=False)
        with open(self.l.KWIN_OUTPUT_CONFIG) as f:
            self.assertEqual(f.read(), "original")


MOONLIGHT_CONF = """\
[General]
bitrate=150000
fps=60
framepacing=true
height=800
vsync=true
width=1280

[hosts]
1\\apps\\1\\name=Desktop
"""


class Moonlight(LauncherTestCase):
    fake_modetest = Display.fake_modetest

    def setUp(self):
        super().setUp()
        self.fake_modetest()
        self.conf = os.path.join(
            self.home,
            ".var",
            "app",
            "com.moonlight_stream.Moonlight",
            "config",
            "Moonlight Game Streaming Project",
            "Moonlight.conf",
        )
        self.l.write_file(self.conf, MOONLIGHT_CONF)
        self.pending = {
            "command": "/usr/bin/flatpak run com.moonlight_stream.Moonlight",
            "moonlight_override": True,
            "moonlight_profiles": {
                "SAM-71B5": {
                    "name": "SAMSUNG",
                    "width": 3840,
                    "height": 2160,
                    "fps": 60,
                    "vsync": False,
                    "framepacing": False,
                    "bitrate": 80000,
                    "hdr": True,
                }
            },
        }

    def conf_values(self):
        return self.l.read_ini_section(self.conf)

    def test_display_identity(self):
        self.assertEqual(self.l.edid_identity(bytes.fromhex("".join(TV_EDID))), ("SAM-71B5", "SAMSUNG"))
        info = self.l.display_info()
        self.assertEqual((info["id"], info["name"]), ("SAM-71B5", "SAMSUNG"))

    def test_override_and_restore(self):
        self.l.override_moonlight(self.pending, self.l.display_info())
        values = self.conf_values()
        self.assertEqual(
            (values["width"], values["height"], values["vsync"], values["framepacing"]),
            ("3840", "2160", "false", "false"),
        )
        self.assertEqual((values["bitrate"], values["hdr"]), ("80000", "true"))
        self.l.restore(reload=False)
        with open(self.conf) as f:
            self.assertEqual(f.read(), MOONLIGHT_CONF)

    def test_overridden_keys_ignore_changes_made_in_moonlight(self):
        self.l.override_moonlight(self.pending, self.l.display_info())
        # Changed in Moonlight during the session: one overridden key, one not.
        self.l.write_ini_values(self.conf, {"bitrate": "23000", "mdns": "false"})
        self.l.restore(reload=False)
        values = self.conf_values()
        self.assertEqual((values["width"], values["bitrate"]), ("1280", "150000"))
        self.assertEqual(values["mdns"], "false")
        # Not in the original file, so restoring removes it again.
        self.assertNotIn("hdr", values)

    def test_fork_only_settings_skip_upstream_moonlight(self):
        profile = self.pending["moonlight_profiles"]["SAM-71B5"]
        profile.update(codec=5, vrr=True)
        self.l.override_moonlight(self.pending, self.l.display_info())
        values = self.conf_values()
        self.assertNotIn("enablevrr", values)
        self.assertNotIn("videocfg", values)
        self.l.restore(reload=False)
        # With the fork's key present, both are applied.
        self.l.write_ini_values(self.conf, {"enablevrr": "false", "videocfg": "2"})
        self.l.override_moonlight(self.pending, self.l.display_info())
        values = self.conf_values()
        self.assertEqual((values["enablevrr"], values["videocfg"]), ("true", "5"))

    def test_settings_file_prefers_the_flatpak(self):
        native = os.path.join(self.home, ".config", "Moonlight Game Streaming Project", "Moonlight.conf")
        self.l.write_file(native, "[General]\nwidth=1920\n")
        self.assertEqual(self.l.moonlight_conf(), self.conf)
        self.assertEqual(self.l.moonlight_conf({"command": "/opt/moonlight/moonlight"}), native)

    def test_nothing_changes_without_the_toggle_or_a_profile(self):
        for pending in (
            dict(self.pending, moonlight_override=False),
            dict(self.pending, moonlight_profiles={}),
            dict(self.pending, command="/usr/bin/retroarch"),
        ):
            self.l.override_moonlight(pending, self.l.display_info())
            with open(self.conf) as f:
                self.assertEqual(f.read(), MOONLIGHT_CONF)


class Combo(LauncherTestCase):
    def report(self, steam, y):
        r = bytearray(64)
        r[0:3] = b"\x01\x00\x09"
        if steam:
            r[14] |= 0x04
        r[50:52] = y.to_bytes(2, "little", signed=True)
        return bytes(r)

    def test_direction(self):
        d = self.l.combo_direction
        self.assertEqual(d(self.report(True, 32767)), 1)
        self.assertEqual(d(self.report(True, -32767)), -1)
        self.assertEqual(d(self.report(True, 5000)), 0)
        self.assertEqual(d(self.report(False, 32767)), 0)

    def test_state_report(self):
        self.assertTrue(self.l.is_deck_state_report(self.report(False, 0)))
        self.assertFalse(self.l.is_deck_state_report(b"\x01\x00\x09"))


class Volume(LauncherTestCase):
    def test_parse(self):
        out = "Volume: front-left: 32768 /  50% / -18.06 dB,   front-right: 32768 /  50% / -18.06 dB\n"
        self.assertEqual(self.l.parse_volume(out, "Mute: no\n"), (50, False))
        self.assertEqual(self.l.parse_volume(out, "Mute: yes\n"), (50, True))
        self.assertIsNone(self.l.parse_volume("", "Mute: no"))

    def test_loading_qml_is_filled_in(self):
        qml = self.l.LOADING_QML % {
            "title": '"t"',
            "message": '"Starting…"',
            "status_url": '"file:///x"',
        }
        self.assertNotIn("%(", qml)
        # Self-drawn spinner: nothing from the desktop theme.
        self.assertNotIn("QtQuick.Controls", qml)

    def test_osd_qml_is_filled_in(self):
        qml = self.l.OSD_QML % {"title": '"t"', "state_url": '"file:///x"'}
        self.assertIn("text: osd.valueText", qml)
        self.assertNotIn("%(", qml)


class LogTrim(LauncherTestCase):
    def test_keeps_only_recent_lines(self):
        self.l.write_file(self.l.LOG, "".join(f"line {i}\n" for i in range(1000)))
        self.l.trim_log(keep=10)
        with open(self.l.LOG) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines, [f"line {i}" for i in range(990, 1000)])

    def test_short_log_untouched(self):
        self.l.write_file(self.l.LOG, "a\nb\n")
        self.l.trim_log(keep=10)
        with open(self.l.LOG) as f:
            self.assertEqual(f.read(), "a\nb\n")


MOONLIGHT_HOSTS_CONF = r"""[General]
width=1280

[hosts]
1\apps\1\hidden=false
1\apps\1\id=749207497
1\apps\1\name="     Desktop"
1\apps\2\hidden=true
1\apps\2\id=2
1\apps\2\name=Secret
1\apps\3\hidden=false
1\apps\3\id=3
1\apps\3\name=Steam Big Picture
1\apps\size=3
1\hostname=star
1\srvcert=@ByteArray(-----BEGIN CERTIFICATE-----)
1\localaddress=192.168.1.73
1\uuid=D2A7582E
size=1
"""


class StreamLaunch(LauncherTestCase):
    def test_hosts_without_secrets_or_hidden_apps(self):
        path = os.path.join(self.home, "Moonlight.conf")
        self.l.write_file(path, MOONLIGHT_HOSTS_CONF)
        hosts = self.l.moonlight_hosts(path)
        self.assertEqual(
            hosts,
            [
                {
                    "name": "star",
                    "uuid": "D2A7582E",
                    "apps": [{"id": 749207497, "name": "     Desktop"}, {"id": 3, "name": "Steam Big Picture"}],
                }
            ],
        )
        self.assertNotIn("192.168", json.dumps(hosts))

    def test_hold_closes_after_every_reason(self):
        closed = []
        self.l.close_loading_screen = closed.append
        hold = self.l.LoadingHold("loading", ["network", "stream"])
        hold.status("network", "Waiting for the network…")
        hold.status("stream", "Starting Desktop…")
        hold.release("network")
        self.assertEqual(closed, [])
        with open(self.l.LOADING_STATUS) as f:
            self.assertEqual(f.read(), "Starting Desktop…")
        hold.release("stream")
        self.assertEqual(closed, ["loading"])

    def run_watcher(self, lines):
        closed, statuses = [], []
        self.l.close_loading_screen = closed.append
        hold = self.l.LoadingHold("loading", ["stream"])
        real_status = hold.status
        hold.status = lambda reason, text: (statuses.append(text), real_status(reason, text))
        released_at = []
        real_release = hold.release

        def release(reason):
            if not released_at:
                released_at.append(len(statuses))
            real_release(reason)

        hold.release = release

        class Proc:
            stdout = iter(lines)

        # Moonlight's output is passed through to stdout (the journal).
        with contextlib.redirect_stdout(io.StringIO()) as passed_through:
            self.l.watch_stream(Proc, hold, {"host": "star", "app": "  Resume"}, time.monotonic())
        self.assertEqual(passed_through.getvalue(), "".join(lines))
        return statuses, closed

    def test_watcher_hands_over_when_video_starts(self):
        statuses, closed = self.run_watcher(
            [
                "00:00:01 - SDL Info (0): Starting RTSP handshake...\n",
                "00:00:02 - SDL Info (0): Video stream is 1920x1200x60 (format 0x10000)\n",
            ]
        )
        self.assertEqual(statuses, ["Connecting to star…", "Starting Resume…"])
        self.assertEqual(closed, ["loading"])

    def test_watcher_hands_over_on_failure(self):
        _, closed = self.run_watcher(["00:00:01 - Qt Critical: Network unreachable (Error 99)\n"])
        self.assertEqual(closed, ["loading"])


class Masking(LauncherTestCase):
    def test_restore_unmasks_in_one_call(self):
        # Each systemctl (un)mask reloads systemd, about half a second.
        calls = []
        self.l.systemctl = lambda *args: calls.append(args) or subprocess.CompletedProcess(args, 0, "")
        units = [self.l.PLASMASHELL_UNIT, *self.l.QUIET_MASKED_UNITS]
        self.l.record_undo("masked", units)
        self.l.restore(reload=False)
        self.assertEqual([c for c in calls if c[0] == "unmask"], [("unmask", "--runtime", *units)])


class Diagnostics(LauncherTestCase):
    def test_redacts_addresses_and_wifi_names(self):
        text = (
            "locked My Home Net to access point BC:51:5F:57:11:68\n"
            '"star" is now online at "192.168.1.73:47989"\nmac bc-51-5f-57-11-6c'
        )
        out = self.l.redact(text, ["My Home Net"])
        self.assertNotIn("BC:51", out)
        self.assertNotIn("bc-51", out)
        self.assertNotIn("192.168", out)
        self.assertNotIn("My Home Net", out)
        self.assertIn("locked <wifi> to access point xx:xx:xx:xx:xx:xx", out)

    def test_report_runs_without_a_deck(self):
        self.l.write_file(self.l.LOG, "2026-10-03 12:00:00 [1] locked Net to access point AA:BB:CC:DD:EE:FF\n")
        report = self.l.diagnostics()
        self.assertIn("launcher.log", report)
        self.assertNotIn("AA:BB", report)


class Uninstall(LauncherTestCase):
    def test_removes_unit_and_generated_files_but_keeps_the_log(self):
        # Decky runs uninstall on updates too: the log must survive them.
        self.l.install_unit()
        self.l.write_file(self.l.LOG, "log\n")
        self.l.write_file(self.l.KWIN_SCRIPT, "// generated\n")
        self.l.write_file(self.l.LOADING_SCREEN, "// generated\n")
        self.l.uninstall()
        self.assertFalse(os.path.exists(os.path.join(self.l.UNIT_DIR, self.l.UNIT_NAME)))
        self.assertFalse(
            os.path.lexists(os.path.join(self.l.UNIT_DIR, f"{self.l.UNIT_TARGET}.wants", self.l.UNIT_NAME))
        )
        self.assertEqual(sorted(os.listdir(self.l.STATE)), [os.path.basename(self.l.LOG)])

    def test_nothing_left_means_no_state_folder(self):
        self.l.uninstall()
        self.assertFalse(os.path.exists(self.l.STATE))


if __name__ == "__main__":
    unittest.main()
