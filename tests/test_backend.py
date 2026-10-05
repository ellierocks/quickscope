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
        self.started = []

        async def fake_start(*args):
            self.started.append(args)

        real_start = main._start_launcher
        main._start_launcher = fake_start
        self.addCleanup(setattr, main, "_start_launcher", real_start)
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
        # The host starts the app while the session switches.
        self.assertEqual(self.started, [("--start-host-app",)])
        asyncio.run(self.plugin.cancel_pending())
        self.assertEqual(self.started[-1], ("--quit-host-app",))

    def test_only_streams_start_a_host_app(self):
        self.stage(SHORTCUT)
        self.assertEqual(self.started, [])

    def test_stream_launch_runs_the_shortcuts_appimage(self):
        # Even with the Flatpak installed: the shortcut is the Moonlight in use.
        os.makedirs(
            os.path.join(TMP, ".local", "share", "flatpak", "app", "com.moonlight_stream.Moonlight"), exist_ok=True
        )
        appimage = os.path.join(TMP, "Applications", "Moonlight-6.1.0-x86_64.AppImage")
        os.makedirs(os.path.dirname(appimage), exist_ok=True)
        with open(appimage, "wb") as f:
            f.write(b"\x7fELF\x02\x01\x01\x00AI\x02")
        spec = {"appid": 4, "name": "Resume", "kind": "stream", "host": "star", "app": "  Resume"}
        spec.update(moonlight_exe=f'"{appimage}"', moonlight_launch_options="QT_QPA_PLATFORM=xcb %command%")
        mode, pending = self.stage(spec)
        self.assertEqual(mode, "hybrid")
        self.assertEqual(pending["command"], f"QT_QPA_PLATFORM=xcb \"{appimage}\" stream star '  Resume'")
        # A Flatpak shortcut doesn't count; the usual lookup applies.
        real_which = main._which
        main._which = lambda cmd: f"/usr/bin/{cmd}" if cmd == "flatpak" else None
        self.addCleanup(setattr, main, "_which", real_which)
        spec.update(moonlight_exe='"/usr/bin/flatpak"', moonlight_launch_options="run com.moonlight_stream.Moonlight")
        self.assertTrue(self.stage(spec)[1]["command"].startswith("flatpak run com.moonlight_stream.Moonlight stream"))

    def test_pinned_streams_keep_exact_names(self):
        asyncio.run(
            self.plugin.set_setting(
                "pinned_streams", [{"host": "star", "app": "  Resume"}, {"host": "", "app": "x"}, {"app": "y"}]
            )
        )
        self.assertEqual(self.plugin.settings["pinned_streams"], [{"host": "star", "app": "  Resume"}])

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


MOONLIGHT_LIST_CSV = """Name, ID, HDR Support, App Collection Game, Hidden, Direct Launch, Boxart URL
"     Desktop",749207497,false,false,false,false,"https://host/a.png"
"Secret",2,false,false,true,false,""
"Cyberpunk, 2077",77,true,false,false,false,""
"""


class MoonlightHostApps(unittest.TestCase):
    def setUp(self):
        self.plugin = main.Plugin()
        self.listed = []
        conf_hosts = [{"name": "star", "uuid": "U", "apps": [{"id": 3, "name": "Old"}], "hidden": [77]}]

        async def fake_launcher(*args):
            return 0, json.dumps(conf_hosts)

        async def fake_list(base, host):
            self.listed.append(host)
            return self.fresh

        real = main._run_launcher, main._moonlight_list, main._moonlight_base
        main._run_launcher, main._moonlight_list = fake_launcher, fake_list
        main._moonlight_base = lambda exe=None: ["moonlight"]
        self.addCleanup(lambda: setattr(main, "_moonlight_base", real[2]))
        self.addCleanup(lambda: (setattr(main, "_run_launcher", real[0]), setattr(main, "_moonlight_list", real[1])))

    def test_parse_keeps_exact_names_and_skips_hidden(self):
        self.assertEqual(
            main.parse_moonlight_list(MOONLIGHT_LIST_CSV),
            [{"id": 749207497, "name": "     Desktop"}, {"id": 77, "name": "Cyberpunk, 2077"}],
        )

    def test_parse_without_csv_header_is_no_answer(self):
        self.assertIsNone(main.parse_moonlight_list("Desktop\nSteam Big Picture\n"))
        self.assertEqual(main.parse_moonlight_list(MOONLIGHT_LIST_CSV.splitlines()[0]), [])

    def test_refresh_replaces_the_saved_apps_but_not_moonlights_hidden_ones(self):
        self.fresh = main.parse_moonlight_list(MOONLIGHT_LIST_CSV)
        hosts = asyncio.run(self.plugin.refresh_moonlight_hosts(None))
        self.assertEqual(self.listed, ["star"])
        self.assertEqual(hosts[0]["apps"], [{"id": 749207497, "name": "     Desktop"}])
        # Later panel opens show the refreshed list straight away.
        self.assertEqual(asyncio.run(self.plugin.get_moonlight_hosts())[0]["apps"], hosts[0]["apps"])

    def test_unreachable_host_keeps_its_apps(self):
        self.fresh = None
        hosts = asyncio.run(self.plugin.refresh_moonlight_hosts(None))
        self.assertEqual(hosts[0]["apps"], [{"id": 3, "name": "Old"}])


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

    EVERYTHING = ("kill", "--signal=SIGKILL", "gamescope-session.service")

    def setUp(self):
        # The session's processes, and which of them got SIGKILL.
        self.killed = []
        real = main._unit_processes, main.os.kill

        async def fake_processes(unit):
            return [(1, "gamescope-session"), (2, "gamescope-wl"), (3, "steam")]

        main._unit_processes = fake_processes
        main.os.kill = lambda pid, sig: self.killed.append(pid)
        self.addCleanup(lambda: (setattr(main, "_unit_processes", real[0]), setattr(main.os, "kill", real[1])))

    def test_quick_stop_is_left_alone(self):
        self.assertEqual(self.run_states(["active", "deactivating", "inactive"]), [])
        self.assertEqual(self.killed, [])

    def test_only_gamescope_itself_is_killed_soon_after_the_stop_begins(self):
        # Steam and the rest of the session still get their SIGTERM and can save.
        self.assertEqual(self.run_states(["active", *["deactivating"] * 5, "inactive"]), [])
        self.assertEqual(self.killed, [2])

    def test_stuck_stop_kills_whatever_is_left(self):
        self.assertEqual(self.run_states(["active", "deactivating"]), [self.EVERYTHING])
        self.assertEqual(self.killed, [2])


if __name__ == "__main__":
    unittest.main()
