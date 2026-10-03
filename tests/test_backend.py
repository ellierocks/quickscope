"""Tests for the Decky backend (main.py), with decky and pwd stubbed out."""

import asyncio
import json
import logging
import os
import pathlib
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp()

decky = types.ModuleType("decky")
decky.DECKY_USER = "quickscope-test-user"
decky.DECKY_USER_HOME = TMP
decky.DECKY_PLUGIN_SETTINGS_DIR = os.path.join(TMP, "settings")
decky.DECKY_PLUGIN_DIR = str(ROOT)
decky.logger = logging.getLogger("quickscope-test")
sys.modules["decky"] = decky
if "pwd" not in sys.modules:  # Windows
    pwd = types.ModuleType("pwd")
    pwd.getpwnam = lambda name: (_ for _ in ()).throw(KeyError(name))
    sys.modules["pwd"] = pwd
if not hasattr(os, "getuid"):
    os.getuid = lambda: 1000

sys.path.insert(0, str(ROOT))
import main  # noqa: E402

SHORTCUT = {
    "appid": 3000000001,
    "name": "Moonlight",
    "kind": "shortcut",
    "exe": '"/usr/bin/flatpak"',
    "launch_options": "run com.moonlight_stream.Moonlight",
}


async def _no_launcher(*args):
    return 0, ""


class BuildDirectCommand(unittest.TestCase):
    def test_quoted_exe_with_options(self):
        self.assertEqual(main.build_direct_command('"/usr/bin/flatpak"', "run x"), '"/usr/bin/flatpak" run x')

    def test_unquoted_exe_with_spaces_is_quoted(self):
        self.assertEqual(main.build_direct_command("/opt/my app/run", ""), "'/opt/my app/run'")

    def test_percent_command(self):
        self.assertEqual(main.build_direct_command('"/x"', "MANGOHUD=0 %command% --fs"), 'MANGOHUD=0 "/x" --fs')

    def test_no_exe(self):
        self.assertIsNone(main.build_direct_command("", "x"))


class PrepareLaunch(unittest.TestCase):
    def setUp(self):
        main._run_launcher = _no_launcher
        os.makedirs(decky.DECKY_PLUGIN_SETTINGS_DIR, exist_ok=True)
        path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
        if os.path.exists(path):
            os.remove(path)
        self.plugin = main.Plugin()
        self.plugin._load_settings()

    def stage(self, spec):
        result = asyncio.run(self.plugin.prepare_launch(spec))
        self.assertTrue(result["ok"], result)
        with open(main._paths()["pending"]) as f:
            return result["mode"], json.load(f)

    def test_shortcut_defaults_to_hybrid(self):
        mode, pending = self.stage(SHORTCUT)
        self.assertEqual(mode, "hybrid")
        self.assertIsNone(pending["gameid"])
        self.assertTrue(pending["command"].startswith('"/usr/bin/flatpak"'))

    def test_direct_override(self):
        asyncio.run(self.plugin.set_setting("launch_modes", {"3000000001": "direct"}))
        self.assertEqual(self.stage(SHORTCUT)[0], "direct")

    def test_shortcut_without_exe_goes_through_steam(self):
        mode, pending = self.stage({"appid": 3000000001, "name": "Moonlight", "kind": "shortcut"})
        self.assertEqual(mode, "steam")
        self.assertEqual(pending["gameid"], (3000000001 << 32) | 0x02000000)

    def test_steam_game_goes_through_steam(self):
        mode, pending = self.stage({"appid": 570, "name": "Dota 2", "kind": "steam"})
        self.assertEqual((mode, pending["gameid"]), ("steam", 570))

    def test_stream_launch_runs_moonlight_stream(self):
        os.makedirs(
            os.path.join(TMP, ".local", "share", "flatpak", "app", "com.moonlight_stream.Moonlight"), exist_ok=True
        )
        real_which = main._which
        main._which = lambda cmd: f"/usr/bin/{cmd}" if cmd == "flatpak" else None
        self.addCleanup(setattr, main, "_which", real_which)
        mode, pending = self.stage({"appid": 4, "name": "Resume", "kind": "stream", "host": "star", "app": "  Resume"})
        self.assertEqual(mode, "hybrid")
        self.assertEqual(pending["command"], "flatpak run com.moonlight_stream.Moonlight stream star '  Resume'")
        self.assertEqual(pending["stream"], {"host": "star", "app": "  Resume"})

    def test_unknown_launch_modes_are_dropped(self):
        asyncio.run(self.plugin.set_setting("launch_modes", {"1": "direct", "2": "steam", "3": "bogus"}))
        self.assertEqual(self.plugin.settings["launch_modes"], {"1": "direct"})

    def test_power_profile_reaches_the_launch(self):
        self.assertEqual(self.stage(SHORTCUT)[1]["power_profile"], "auto")
        asyncio.run(self.plugin.set_setting("power_profile", "battery"))
        self.assertEqual(self.stage(SHORTCUT)[1]["power_profile"], "battery")
        with self.assertRaises(ValueError):
            asyncio.run(self.plugin.set_setting("power_profile", "turbo"))

    def test_old_performance_toggle_migrates(self):
        with open(self.plugin._settings_path(), "w") as f:
            json.dump({"performance": False}, f)
        self.plugin._load_settings()
        self.assertEqual(self.plugin.settings["power_profile"], "battery")
        self.assertNotIn("performance", self.plugin.settings)

    def test_settings_migration(self):
        with open(self.plugin._settings_path(), "w") as f:
            json.dump(
                {"direct_nonsteam": False, "skip_desktop_steam": True, "launch_modes": {"5": "steam", "6": "direct"}}, f
            )
        self.plugin._load_settings()
        self.assertEqual(self.plugin.settings["launch_modes"], {"6": "direct"})
        self.assertNotIn("direct_nonsteam", self.plugin.settings)


class GamescopeWatchdog(unittest.TestCase):
    def run_states(self, states):
        calls, it = [], iter(states)

        async def fake_systemctl(*args):
            calls.append(args)
            return 0, next(it, states[-1]) if args[0] == "show" else ""

        original = main._systemctl_user, main.GAMESCOPE_STOP_GRACE
        main._systemctl_user, main.GAMESCOPE_STOP_GRACE = fake_systemctl, 0.6
        try:
            asyncio.run(main._hurry_gamescope_stop())
        finally:
            main._systemctl_user, main.GAMESCOPE_STOP_GRACE = original
        return [c for c in calls if c[0] == "kill"]

    def test_normal_stop_is_left_alone(self):
        self.assertEqual(self.run_states(["active", "deactivating", "deactivating", "inactive"]), [])

    def test_stuck_stop_is_killed_once(self):
        self.assertEqual(
            self.run_states(["active", "deactivating"]), [("kill", "--signal=SIGKILL", "gamescope-session.service")]
        )


if __name__ == "__main__":
    unittest.main()
