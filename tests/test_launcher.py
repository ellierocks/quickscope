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
