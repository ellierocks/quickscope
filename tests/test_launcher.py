"""Tests for defaults/quickscope_launcher.py, run against a temporary HOME."""
import importlib.util
import os
import pathlib
import shutil
import subprocess
import tempfile
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
              .replace("%LOADING_PID%", "1234").replace("%FORCE_FULLSCREEN%", "true"))
        self.assertNotIn("%", js)
        path = os.path.join(self.home, "session.js")
        with open(path, "w") as f:
            f.write(js)
        result = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


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
