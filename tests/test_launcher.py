"""Tests for defaults/quickscope_launcher.py, run against a temporary HOME."""
import importlib.util
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
        for name, exe in [("baloo_file.desktop", "baloo_file"), ("steam.desktop", "/usr/bin/steam -silent %U"),
                          ("org.kde.kdeconnect.daemon.desktop", "kdeconnectd"),
                          ("polkit-kde-authentication-agent-1.desktop", "polkit")]:
            self.write_entry(system, name, exe)
        user_app = "com.github.zocker_160.SyncThingy.desktop"
        self.write_entry(self.l.AUTOSTART_DIR, user_app, "flatpak run x")

        hidden = self.l.hide_autostart(lambda name, entry, is_user: (
            self.l.is_steam_entry(entry) or (not is_user and name in self.l.QUIET_AUTOSTART)))

        self.assertEqual(sorted(os.path.basename(h["path"]) for h in hidden),
                         ["baloo_file.desktop", "org.kde.kdeconnect.daemon.desktop", "steam.desktop"])
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
        js = (self.l.SESSION_JS.replace("%LOADING_TITLE%", self.l.LOADING_TITLE)
              .replace("%OSD_TITLE%", self.l.OSD_TITLE)
              .replace("%LOADING_PID%", "1234").replace("%FORCE_FULLSCREEN%", "true"))
        self.assertNotIn("%", js)
        path = os.path.join(self.home, "session.js")
        with open(path, "w") as f:
            f.write(js)
        result = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


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
        self.state = {"gpu": "auto", "governor": "powersave", "boost": "enabled"}
        commands = {cmd: (key, i) for key, cmds in self.l.POWER_SETTINGS.items() for i, cmd in enumerate(cmds)}

        def fake_run(cmd):
            key, is_set = commands[cmd[1]]
            if is_set:
                self.state[key] = cmd[2]
            return subprocess.CompletedProcess(cmd, 0, f"Something: {self.state[key]}\n")

        self.l.run = fake_run
        real_which = shutil.which
        shutil.which = lambda name: "/usr/bin/" + name
        self.addCleanup(setattr, shutil, "which", real_which)

    def test_battery_profile_applies_and_restores(self):
        applied = self.l.apply_power_profile("battery")
        self.assertEqual(applied, {"gpu": "auto", "governor": "powersave", "boost": "disabled"})
        self.assertEqual(self.state["boost"], "disabled")
        self.l.restore(reload=False)
        self.assertEqual(self.state, {"gpu": "auto", "governor": "powersave", "boost": "enabled"})

    def test_performance_profile(self):
        self.l.apply_power_profile("performance")
        self.assertEqual(self.state, {"gpu": "high", "governor": "performance", "boost": "enabled"})

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


class Combo(LauncherTestCase):
    def report(self, steam, y):
        r = bytearray(64)
        r[0:3] = b"\x01\x00\x09"
        if steam:
            r[9] |= 0x20
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


class Uninstall(LauncherTestCase):
    def test_removes_unit_and_state(self):
        self.l.install_unit()
        self.l.write_file(self.l.LOG, "log\n")
        self.l.uninstall()
        self.assertFalse(os.path.exists(os.path.join(self.l.UNIT_DIR, self.l.UNIT_NAME)))
        self.assertFalse(os.path.lexists(os.path.join(self.l.UNIT_DIR, f"{self.l.UNIT_TARGET}.wants", self.l.UNIT_NAME)))
        self.assertFalse(os.path.exists(self.l.STATE))


if __name__ == "__main__":
    unittest.main()
