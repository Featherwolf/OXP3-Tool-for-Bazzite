import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from oxp3_tool import templates
from oxp3_tool.cli import main
from oxp3_tool.detect import Context

from .fakeroot import FakeOxp3

PREFIX = "gamescope-session-plus[2100]: [gamescope] [Info]  drm: Got known display: "
OURS = PREFIX + "oxp3_tool_sdc_oled (ONEXPLAYER 3 OLED (oxp3-tool))\n"
LEGION = PREFIX + "lenovo_legiongo2_oled (Legion Go 2)\n"


class KnownDisplayTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.f = FakeOxp3(Path(tmp.name))

    def status(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            main(["status"], system=self.f.system())
        return out.getvalue()

    def test_newest_match_wins(self):
        self.f.runner.session_journal = OURS + LEGION
        self.assertEqual(Context(self.f.system()).gamescope_known_display, "lenovo_legiongo2_oled")
        self.f.runner.session_journal = LEGION + OURS
        self.assertEqual(Context(self.f.system()).gamescope_known_display, "oxp3_tool_sdc_oled")
        self.f.runner.session_journal = ""
        self.assertIsNone(Context(self.f.system()).gamescope_known_display)

    def test_oled_profile_status(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["apply", "--yes", "oled-profile"], system=self.f.system()), 0)
        self.f.runner.session_journal = OURS
        self.assertIn("Game Mode used this profile this boot", self.status())
        self.f.runner.session_journal = LEGION
        self.assertIn("Game Mode used the 'lenovo_legiongo2_oled' display profile this boot", self.status())

    def test_profile_key_matches_lua(self):
        self.assertIn(f"gamescope.config.known_displays.{templates.OLED_DISPLAY_KEY} = {{", templates.oled_lua())


if __name__ == "__main__":
    unittest.main()
