import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from oxp3_tool import templates
from oxp3_tool.cli import main
from oxp3_tool.detect import Context
from oxp3_tool.fixes import BY_ID

from .fakeroot import FakeOxp3

PREFIX = "gamescope-session-plus[2100]: [gamescope] [Info]  drm: Got known display: "
OURS = PREFIX + "oxp3_tool_sdc_oled (ONEXPLAYER 3 OLED (oxp3-tool))\n"
LEGION = PREFIX + "lenovo_legiongo2_oled (Legion Go 2)\n"


class GamescopeHdrTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.f = FakeOxp3(self.root)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(argv), system=self.f.system())
        return rc, out.getvalue(), err.getvalue()

    def check(self):
        return BY_ID["gamescope-hdr"].check(Context(self.f.system()))

    def test_needs_oled_profile_in_use(self):
        check = self.check()
        self.assertFalse(check.applicable)
        self.assertIn("needs oled-profile", check.reason)
        self.assertEqual(self.cli("apply", "--yes", "oled-profile")[0], 0)
        self.assertIn("start Game Mode once", self.check().reason)
        self.f.runner.session_journal = OURS + LEGION
        self.assertIn("'lenovo_legiongo2_oled'", self.check().reason)
        self.f.runner.session_journal = LEGION + OURS
        check = self.check()
        self.assertTrue(check.applicable)
        self.assertFalse(check.recommended)

    def test_no_game_mode(self):
        f = FakeOxp3(self.root / "desktop", game_mode=False)
        self.assertFalse(BY_ID["gamescope-hdr"].check(Context(f.system())).applicable)

    def test_apply_and_revert(self):
        self.cli("apply", "--yes", "oled-profile")
        self.f.runner.session_journal = OURS
        rc, out, err = self.cli("apply", "--yes", "gamescope-hdr")
        self.assertEqual(rc, 0, err)
        env = self.root / templates.GAMESCOPE_HDR_ENV.lstrip("/")
        self.assertEqual(env.read_text(), templates.GAMESCOPE_HDR_CONF)
        self.assertIn("\nENABLE_GAMESCOPE_HDR=1\n", env.read_text())
        rc, out, _ = self.cli("status")
        self.assertIn("[applied     ] gamescope-hdr", out)
        self.assertIn("Game Mode used this profile this boot", out)
        rc, out, err = self.cli("revert", "--yes", "gamescope-hdr")
        self.assertEqual(rc, 0, err)
        self.assertFalse(env.exists())

    def test_profile_key_matches_lua(self):
        self.assertIn(f"gamescope.config.known_displays.{templates.OLED_DISPLAY_KEY} = {{", templates.oled_lua())

    def test_wrong_profile_is_flagged_on_oled_profile(self):
        self.cli("apply", "--yes", "oled-profile")
        self.f.runner.session_journal = LEGION
        _, out, _ = self.cli("status")
        self.assertIn("Game Mode used the 'lenovo_legiongo2_oled' display profile this boot", out)


if __name__ == "__main__":
    unittest.main()
