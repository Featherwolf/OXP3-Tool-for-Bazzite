"""The speaker-profile fix: its PipeWire drop-in, apply/revert in a fake root, and the live checks from pw-dump."""

import contextlib
import io
import json
import math
import re
import tempfile
import unittest
from pathlib import Path

from oxp3_tool import audio, cli, templates
from oxp3_tool.detect import Context
from oxp3_tool.engine import Engine, EngineError
from oxp3_tool.fixes import BY_ID, OPTIONAL
from oxp3_tool.state import STATE_DIR, STATE_FILE, State

from .fakeroot import OXP3_DMI, SPEAKER, FakeCachyOS, FakeOxp3, pw_dump_json
from .pipewire_sim import BUILTIN_PORTS_1_4, biquad_response_db, delay_samples, parse_spa_json, run_graph

CONF = templates.SPEAKER_PROFILE_PATH
FIX = BY_ID["speaker-profile"]


def restarts(runner):
    return [c for c in runner.calls if c[:2] == ["systemctl", "--user"] and "try-restart" in c]


class TemplateTest(unittest.TestCase):
    def setUp(self):
        self.text = templates.speaker_profile_conf()

    def test_marker_and_module(self):
        self.assertIn(templates.OWN_MARKER, self.text.splitlines()[0])
        self.assertIn("name = libpipewire-module-filter-chain", self.text)
        self.assertIn("flags = [ nofail ]", self.text)
        self.assertEqual(self.text.count("{"), self.text.count("}"))
        self.assertEqual(self.text.count("["), self.text.count("]"))

    def test_graph_is_consistent(self):
        names = re.findall(r"name = (\S+) label", self.text)
        self.assertEqual(len(names), 83)
        self.assertEqual(len(names), len(set(names)), "node names are unique")
        inputs = re.findall(r'input = "([^"]+)"', self.text)
        self.assertEqual(len(inputs), 115)
        self.assertEqual(len(inputs), len(set(inputs)), "an input port is linked at most once")
        for node in re.findall(r'(?:output|input) = "([^":]+):', self.text):
            self.assertIn(node, names)
        graph_inputs = re.findall(r'"(\w+):In"', self.text.split("inputs  =")[1].splitlines()[0])
        self.assertEqual(graph_inputs, ["hp1_l", "hp1_r"])
        self.assertIn('outputs = [ "ceil_l:Out" "ceil_r:Out" ]', self.text)
        for i in graph_inputs:
            self.assertNotIn(f'input = "{i}:In"', self.text, "a graph input is not linked inside the graph too")

    def test_graph_is_acyclic(self):
        nodes, links = templates.speaker_profile_graph()
        edges = {}
        for o, i in links:
            edges.setdefault(o.split(":")[0], set()).add(i.split(":")[0])
        seen, done = set(), set()

        def visit(n):
            self.assertNotIn(n, seen - done, f"cycle through {n}")
            if n in done:
                return
            seen.add(n)
            for m in edges.get(n, ()):
                visit(m)
            done.add(n)
        for n in nodes:
            visit(n["name"])

    def test_only_builtin_labels(self):
        labels = set(re.findall(r"label = (\S+)", self.text))
        # Every label exists in PipeWire 1.4.0's and 1.6's builtin filter-graph plugin.
        self.assertLessEqual(labels, {"bq_highpass", "bq_peaking", "bq_highshelf", "bq_lowpass", "linear", "abs",
                                      "delay", "mult", "clamp", "max", "sine", "recip"})
        self.assertEqual(set(re.findall(r"type = (\S+)", self.text)), {"builtin"})

    def test_parameters(self):
        t = self.text
        self.assertEqual(t.count('label = bq_highpass control = { "Freq" = 180.0 "Q" = 0.5412 }'), 2)
        self.assertEqual(t.count('label = bq_highpass control = { "Freq" = 180.0 "Q" = 1.3066 }'), 2)
        self.assertEqual(t.count('"Freq" = 300.0 "Q" = 0.9 "Gain" = 2.0'), 2)
        self.assertEqual(t.count('"Freq" = 3000.0 "Q" = 1.0 "Gain" = 2.5'), 2)
        self.assertEqual(t.count('label = bq_highshelf control = { "Freq" = 8000.0 "Q" = 0.707 "Gain" = 2.0 }'), 2)
        self.assertEqual(t.count('"Mult" = 2.511886 "Add" = 0.0'), 2)  # +8 dB
        self.assertEqual(t.count('"Min" = -0.966051 "Max" = 0.966051'), 2)  # -0.3 dBFS
        self.assertIn('"Offset" = 0.841395', t)  # -1.5 dBFS threshold
        self.assertEqual(t.count('"Delay (s)" = 0.013343750'), 2)  # 640 samples look-ahead
        # Every delay has a max-delay large enough for its delay.
        for cfg, ctl in re.findall(r'config = \{ "max-delay" = ([0-9.]+) \} control = \{ "Delay \(s\)" = ([0-9.]+)', t):
            self.assertGreater(float(cfg), float(ctl))
        self.assertEqual(len(re.findall(r"label = delay config", t)), 2 + 6 + 6 + 6 + 6 + 5)

    def test_routing(self):
        t = self.text
        self.assertIn(f'filter.smart.target = {{ node.name = "{SPEAKER}" }}', t)
        self.assertIn("filter.smart        = true", t)
        self.assertIn("priority.session    = 0", t)
        self.assertIn('node.name           = "oxp3_speaker_profile"', t)
        self.assertIn('node.description = "OXP3 Speakers"', t)
        playback = t.split("playback.props")[1]
        for key in ("node.passive       = true", "node.dont-fallback = true", "node.linger        = true"):
            self.assertIn(key, playback)
        for bad in ("node.target", "target.object", "priority.driver"):
            self.assertNotIn(bad, t)

    def test_other_speaker_names(self):
        other = "alsa_output.pci-0000_00_1f.3-platform-sof_sdw.HiFi__Speaker__sink"
        self.assertIn(f'node.name = "{other}"', templates.speaker_profile_conf(other))
        for bad in ('x" } } ] evil', "alsa_output.pci.HiFi__Headphones__sink", ""):
            with self.assertRaises(ValueError):
                templates.speaker_profile_conf(bad)


RATES = (44100, 48000, 88200, 96000, 176400, 192000)


class PipeWireConfigTest(unittest.TestCase):
    """The drop-in as PipeWire 1.4-1.6 reads it: SPA-JSON syntax, then the filter graph against the builtin plugin."""

    def setUp(self):
        self.conf = parse_spa_json(templates.speaker_profile_conf())
        self.module = self.conf["context.modules"][0]
        self.args = self.module["args"]
        self.graph = self.args["filter.graph"]

    def test_parses_like_pipewire(self):
        self.assertEqual(list(self.conf), ["context.modules"])
        self.assertEqual(self.module["name"], "libpipewire-module-filter-chain")
        self.assertEqual(self.module["flags"], ["nofail"])
        capture, playback = self.args["capture.props"], self.args["playback.props"]
        self.assertEqual(capture["media.class"], "Audio/Sink")
        self.assertEqual(capture["filter.smart"], "true")
        self.assertEqual(capture["filter.smart.target"], {"node.name": SPEAKER})
        self.assertEqual(playback["node.dont-fallback"], "true")
        self.assertEqual(playback["node.linger"], "true")
        self.assertNotIn("media.class", playback)  # a stream: WirePlumber links it to the speaker, nothing else
        for bad in ('a = { b = "x" c = [ 1 2 }', "a = { b }", "a = [ 1 = 2 ]", 'a = "x', "a = { b = 1 } }"):
            with self.assertRaises(ValueError, msg=bad):
                parse_spa_json(bad)

    def test_ports_exist_in_pipewire_1_4_and_1_6(self):
        nodes = {n["name"]: n for n in self.graph["nodes"]}
        for n in nodes.values():
            ports = BUILTIN_PORTS_1_4[n["label"]]
            self.assertEqual(n["type"], "builtin")
            self.assertLessEqual(set(n.get("control", {})), set(ports["control"]), n["name"])
            self.assertLessEqual(set(n.get("config", {})), set(ports.get("config", [])), n["name"])
        for link in self.graph["links"]:
            o_node, o_port = link["output"].split(":", 1)
            i_node, i_port = link["input"].split(":", 1)
            self.assertIn(o_port, BUILTIN_PORTS_1_4[nodes[o_node]["label"]]["out"], link)
            self.assertIn(i_port, BUILTIN_PORTS_1_4[nodes[i_node]["label"]]["in"], link)
        for p in self.graph["inputs"]:
            self.assertIn(p.split(":", 1)[1], BUILTIN_PORTS_1_4[nodes[p.split(":", 1)[0]]["label"]]["in"])
        for p in self.graph["outputs"]:
            self.assertEqual(nodes[p.split(":", 1)[0]]["label"], "clamp", "the last stage is the hard ceiling")

    def test_high_pass(self):
        hp = [n for n in self.graph["nodes"] if n["name"] in ("hp1_l", "hp2_l")]
        self.assertEqual(len(hp), 2)

        def response(f):
            return sum(biquad_response_db(n["label"], 48000, float(n["control"]["Freq"]), float(n["control"]["Q"]),
                                          0.0, f) for n in hp)
        self.assertAlmostEqual(response(180.0), -3.0, delta=0.1)  # Butterworth corner
        self.assertLess(response(90.0), -23.0)  # 24 dB per octave
        self.assertLess(response(50.0), -43.0)
        self.assertGreater(response(1000.0), -0.01)

    def test_hold_window_has_no_gaps_at_any_rate(self):
        # The limiter only works if the running maximum covers every sample from now back to beyond the
        # look-ahead, at whatever rate the graph runs.
        nodes = {n["name"]: n for n in self.graph["nodes"]}
        feeds = {}
        for link in self.graph["links"]:
            feeds.setdefault(link["input"].split(":")[0], []).append(link["output"].split(":")[0])
        for rate in RATES:
            def lags(name, rate=rate):
                if name == "peak0":
                    return {0}
                n = nodes[name]
                own = delay_samples(n, rate) if n["label"] == "delay" else 0
                return {own + lag for src in feeds[name] for lag in lags(src, rate)}
            covered = lags("hold5")
            span = max(covered)
            with self.subTest(rate=rate):
                self.assertEqual(covered, set(range(span + 1)), "no sample missing from the hold")
                look = delay_samples(nodes["look_l"], rate)
                self.assertEqual(look, delay_samples(nodes["look_r"], rate))
                self.assertGreater(span, 2.5 * look)
                self.assertAlmostEqual(look / rate, 640 / 48000, delta=1 / 44100)


class LimiterTest(unittest.TestCase):
    """The graph run sample by sample: loud input never reaches the hard ceiling, quiet input is left alone."""

    @classmethod
    def setUpClass(cls):
        cls.graph = parse_spa_json(templates.speaker_profile_conf())["context.modules"][0]["args"]["filter.graph"]

    def run_both(self, left, right, rate):
        (out_l, out_r), nodes = run_graph(self.graph, [left, right], rate)
        return out_l, out_r, nodes

    def signals(self, rate):
        n = rate // 5
        noise = [((i * 2654435761) % 4294967296) / 2147483648 - 1 for i in range(n)]  # full-scale, deterministic
        return {
            "full-scale square 1 kHz": [1.0 if (i * 1000 // rate) % 2 == 0 else -1.0 for i in range(n)],
            "impulses": [1.0 if i % 1201 == 600 else 0.0 for i in range(n)],
            "full-scale noise": noise,
            "silence, then full-scale 3 kHz": [0.0 if i < n // 2 else math.sin(2 * math.pi * 3000 * i / rate)
                                               for i in range(n)],
            "alternating +1/-1": [1.0 if i % 2 else -1.0 for i in range(n)],
        }

    def test_loud_input_stays_below_the_ceiling(self):
        ceiling = 10 ** (templates.SPEAKER_CEILING_DB / 20)
        for rate in (48000, 44100, 96000):
            for name, sig in self.signals(rate).items():
                with self.subTest(rate=rate, signal=name):
                    out_l, out_r, nodes = self.run_both(sig, [-v for v in sig], rate)
                    before = max(max(map(abs, nodes["vca_l"])), max(map(abs, nodes["vca_r"])))
                    self.assertLess(before, ceiling * 10 ** (-0.5 / 20), "the limiter, not the clamp, holds it")
                    self.assertLessEqual(max(max(map(abs, out_l)), max(map(abs, out_r))), ceiling)
                    gain = [a * b for a, b in zip(nodes["inv"], nodes["thresh"], strict=True)]
                    self.assertLessEqual(max(gain), 1.0 + 1e-6, "the limiter never adds gain")

    def test_quiet_input_untouched(self):
        rate = 48000
        sig = [0.05 * math.sin(2 * math.pi * 1000 * i / rate) for i in range(rate // 10)]
        _, _, nodes = self.run_both(sig, sig, rate)
        self.assertEqual({round(a * b, 6) for a, b in zip(nodes["inv"], nodes["thresh"], strict=True)}, {1.0})
        # Then +8 dB, plus the EQ at 1 kHz (the 3 kHz peak's skirt): louder, and only delayed by the look-ahead.
        out = nodes["vca_l"][rate // 20:]
        gain_db = 20 * math.log10(max(map(abs, out)) / 0.05)
        self.assertGreater(gain_db, templates.SPEAKER_MAKEUP_DB)
        self.assertLess(gain_db, templates.SPEAKER_MAKEUP_DB + 1.5)

    def test_output_never_above_the_ceiling_even_if_clamp_is_reached(self):
        # The clamp is the last node, so nothing above it ever leaves the filter, even for input above 0 dBFS.
        rate = 48000
        sig = [4.0 if i % 300 == 0 else 0.0 for i in range(rate // 10)]
        out_l, out_r, _ = self.run_both(sig, sig, rate)
        self.assertLessEqual(max(map(abs, out_l + out_r)), 10 ** (templates.SPEAKER_CEILING_DB / 20) + 1e-6)


class ParseTest(unittest.TestCase):
    def test_parse(self):
        g = audio.parse_pw_dump(pw_dump_json(volume=3.375, default="oxp3_speaker_profile", headphones=True))
        self.assertEqual(g.speaker_sinks(), [SPEAKER])
        self.assertTrue(g.linked("oxp3_speaker_profile.output", SPEAKER))
        self.assertAlmostEqual(g.volume(SPEAKER), 3.375)
        self.assertEqual(audio.percent(3.375), 150)
        self.assertEqual(g.defaults["default.configured.audio.sink"], "oxp3_speaker_profile")

    def test_garbage(self):
        for text in ("", "{", "{}", "nope", "[1, 2]"):
            g = audio.parse_pw_dump(text)
            self.assertTrue(g is None or not g.nodes)


class FixTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def fake(self, cls=FakeOxp3, **kw):
        self.f = cls(self.root, **kw)
        return self.f

    def engine(self):
        ctx = Context(self.f.system())
        return Engine(ctx, State(self.f.system()), out=lambda _: None), ctx

    def apply(self, **opts):
        eng, _ = self.engine()
        return eng.apply(eng.plan([FIX], opts))

    def revert(self):
        eng, _ = self.engine()
        return eng.revert([FIX.id])

    def path(self, p):
        return self.root / p.lstrip("/")

    def state(self):
        return json.loads(self.path(STATE_FILE).read_text())["fixes"][FIX.id]

    def status_lines(self):
        ctx = Context(self.f.system())
        return cli._fix_state(ctx, State(self.f.system(), tolerant=True), FIX)

    # -- check ---------------------------------------------------------------

    def test_optional_never_recommended(self):
        self.fake()
        _, ctx = self.engine()
        check = FIX.check(ctx)
        self.assertTrue(check.applicable)
        self.assertFalse(check.recommended)
        self.assertEqual(FIX.level, OPTIONAL)
        self.assertEqual(self.status_lines()[0], "optional")

    def test_needs_pipewire_pieces(self):
        for missing, why in (("/usr/bin/pipewire", "PipeWire is not installed"),
                             ("/usr/lib64/spa-0.2/filter-graph/libspa-filter-graph-plugin-builtin.so", "1.4"),
                             ("/usr/share/wireplumber/scripts/linking/find-filter-target.lua", "WirePlumber 0.5")):
            with self.subTest(missing=missing):
                self.fake()
                self.path(missing).unlink()
                _, ctx = self.engine()
                check = FIX.check(ctx)
                self.assertFalse(check.applicable)
                self.assertIn(why, check.reason)

    def test_refused_on_another_device(self):
        self.fake(dmi={**OXP3_DMI, "product_name": "ONEXPLAYER X1"})
        eng, _ = self.engine()
        with self.assertRaises(EngineError):
            eng.plan([FIX], {})
        self.assertFalse(self.path(CONF).exists())

    # -- apply / revert -------------------------------------------------------

    def test_apply_and_revert(self):
        self.fake()
        self.f.login("gamer", 1000, pw_dump_json(filter_loaded=False))
        out = self.apply()
        self.assertTrue(out.ok, out.errors)
        self.assertEqual(self.path(CONF).read_text(), templates.speaker_profile_conf(SPEAKER))
        entry = self.state()
        self.assertEqual([r["path"] for r in entry["files"]], [CONF])
        self.assertIsNone(entry["files"][0]["backup"])
        self.assertEqual(entry["options"], {"speaker_node": SPEAKER})
        self.assertEqual(entry["dirs_created"], ["/etc/pipewire", "/etc/pipewire/pipewire.conf.d"])
        self.assertEqual(restarts(self.f.runner), [[
            "systemctl", "--user", "-M", "gamer@", "try-restart", "pipewire.service", "pipewire-pulse.service",
            "wireplumber.service"]])
        self.assertTrue(any("PipeWire restarts" in m for m in out.messages))

        self.f.runner.calls.clear()
        out = self.apply()  # nothing changed: no restart
        self.assertFalse(out.changed)
        self.assertEqual(restarts(self.f.runner), [])

        self.f.runner.calls.clear()
        out = self.revert()
        self.assertTrue(out.ok, out.errors)
        self.assertFalse(self.path(CONF).exists())
        self.assertFalse(self.path("/etc/pipewire").exists(), "the directories it created are gone")
        self.assertFalse(self.path(STATE_FILE).exists())
        self.assertEqual(len(restarts(self.f.runner)), 1)

    def test_existing_directory_and_files_kept(self):
        self.fake()
        self.f.mkdir("/etc/pipewire/pipewire.conf.d")
        self.f.write("/etc/pipewire/pipewire.conf.d/10-mine.conf", "# mine\n")
        self.assertTrue(self.apply().ok)
        self.assertEqual(self.state()["dirs_created"], [])
        self.assertTrue(self.revert().ok)
        self.assertTrue(self.path("/etc/pipewire/pipewire.conf.d").is_dir())
        self.assertEqual(self.path("/etc/pipewire/pipewire.conf.d/10-mine.conf").read_text(), "# mine\n")
        self.assertFalse(self.path(CONF).exists())

    def test_empty_existing_directory_kept(self):
        self.fake()
        self.f.mkdir("/etc/pipewire/pipewire.conf.d")  # the owner's machine: exists and is empty
        self.assertTrue(self.apply().ok)
        self.assertTrue(self.revert().ok)
        self.assertTrue(self.path("/etc/pipewire/pipewire.conf.d").is_dir())

    def test_no_restart_and_nobody_logged_in(self):
        self.fake()
        self.f.login("gamer", 1000)
        out = self.apply(restart=False)
        self.assertEqual(restarts(self.f.runner), [])
        self.assertTrue(any("log out and back in" in m for m in out.messages))
        self.revert()
        self.fake()
        (self.root / "run/user/1000").rmdir()
        out = self.apply()
        self.assertEqual(restarts(self.f.runner), [])
        self.assertTrue(any("Nobody is logged in" in m for m in out.messages))

    def test_each_user_restarted(self):
        self.fake()
        self.f.login("gamer", 1000)
        self.f.login("kid", 1001)
        self.f.mkdir("/run/user/0")  # root and system users are not sessions
        self.f.mkdir("/run/user/965")
        self.apply()
        self.assertEqual([c[3] for c in restarts(self.f.runner)], ["gamer@", "kid@"])

    def test_edited_file(self):
        self.fake()
        self.apply()
        self.path(CONF).write_text("# my own\n")
        state, notes = self.status_lines()
        self.assertEqual(state, "damaged")
        self.assertIn(f"{CONF} was changed by something else", notes)
        out = self.revert()
        self.assertTrue(out.ok)
        self.assertFalse(self.path(CONF).exists())
        kept = self.path(f"{STATE_DIR}/modified/speaker-profile{CONF}")
        self.assertEqual(kept.read_text(), "# my own\n", "the edited copy is kept")

    def test_missing_file(self):
        self.fake()
        self.apply()
        self.path(CONF).unlink()
        self.assertEqual(self.status_lines()[0], "damaged")

    def test_speaker_name_from_pipewire(self):
        self.fake()
        other = "alsa_output.pci-0000_00_1f.3-platform-sof_sdw.HiFi__Speaker__sink"
        self.f.login("gamer", 1000, pw_dump_json().replace(SPEAKER, other))
        self.apply()
        self.assertIn(f'node.name = "{other}"', self.path(CONF).read_text())
        self.assertEqual(self.state()["options"]["speaker_node"], other)
        # Headphones in (no speaker node): the recorded name is kept.
        self.f.runner.pw_dump["gamer"] = pw_dump_json(speaker=False, headphones=True)
        out = self.apply()
        self.assertFalse(out.changed)
        self.assertIn(other, self.path(CONF).read_text())

    def test_usb_speaker_is_not_the_oxp3s(self):
        # Headphones in (no built-in speaker node) and a USB device with a UCM "Speaker" output: the profile stays
        # on the built-in speaker's name.
        self.fake()
        usb = "alsa_output.usb-Generic_USB_Audio-00.HiFi__Speaker__sink"
        dump = json.loads(pw_dump_json(speaker=False, headphones=True))
        dump.append({"id": 60, "type": "PipeWire:Interface:Node",
                     "info": {"props": {"node.name": usb, "media.class": "Audio/Sink"}}})
        self.f.login("gamer", 1000, json.dumps(dump))
        self.assertTrue(self.apply().ok)
        self.assertNotIn(usb, self.path(CONF).read_text())
        self.assertEqual(self.state()["options"]["speaker_node"], SPEAKER)
        with self.assertRaises(ValueError):
            templates.speaker_profile_conf(usb)

    def test_status_when_installed_for_another_speaker_name(self):
        # Installed while nobody was logged in (the default name), on a unit whose speaker is named differently.
        self.fake()
        self.apply()
        other = "alsa_output.pci-0000_00_1f.3-platform-sof_sdw.HiFi__Speaker__sink"
        self.f.login("gamer", 1000, pw_dump_json(linked=False).replace(SPEAKER, other))
        _, notes = self.status_lines()
        self.assertTrue(any(n.startswith(f"warning: gamer's speaker output is {other}") and
                            "apply speaker-profile` again" in n for n in notes), notes)
        self.assertFalse(any("not the active output" in n for n in notes), notes)
        self.apply()  # now PipeWire answers: the profile follows the real name
        self.assertIn(other, self.path(CONF).read_text())

    def test_note_uses_the_speaker_pipewire_has(self):
        self.fake()
        other = "alsa_output.pci-0000_00_1f.3-platform-sof_sdw.HiFi__Speaker__sink"
        self.f.login("gamer", 1000, pw_dump_json(filter_loaded=False, volume=3.375).replace(SPEAKER, other))
        _, notes = self.status_lines()
        self.assertTrue(any("speaker volume is 150%" in n for n in notes), notes)

    def test_cachyos(self):
        self.fake(FakeCachyOS)
        self.assertTrue(self.path("/usr/lib/spa-0.2/filter-graph/libspa-filter-graph-plugin-builtin.so").exists())
        self.f.login("gamer", 1000, pw_dump_json(volume=3.375))
        self.assertTrue(self.path("/home/gamer").is_dir())
        out = self.apply()
        self.assertTrue(out.ok, out.errors)
        self.assertTrue(self.path(CONF).exists())
        self.assertTrue(any("150%" in m and "wpctl set-volume @DEFAULT_AUDIO_SINK@ 1.0" in m for m in out.messages))

    # -- status ----------------------------------------------------------------

    def test_status_active(self):
        self.fake()
        self.f.login("gamer", 1000, pw_dump_json())
        self.apply()
        state, notes = self.status_lines()
        self.assertEqual(state, "applied")
        self.assertIn("gamer: active, sound for the speakers goes through OXP3 Speakers", notes)
        self.assertFalse([n for n in notes if n.startswith("warning:")], notes)

    def test_status_states(self):
        self.fake()
        self.f.login("gamer", 1000, pw_dump_json())
        self.apply()
        for dump, expect in (
            (pw_dump_json(filter_loaded=False), "warning: gamer's PipeWire has not loaded the profile"),
            (pw_dump_json(speaker=False, headphones=True), "gamer: loaded; the speakers are not the active output"),
            (pw_dump_json(linked=False), "gamer: loaded, not linked to the speakers yet"),
            (pw_dump_json(volume=3.375), "warning: gamer's speaker volume is 150%"),
            (pw_dump_json(default="oxp3_speaker_profile"), 'warning: gamer chose "OXP3 Speakers" as the output'),
        ):
            with self.subTest(expect=expect):
                self.f.runner.pw_dump["gamer"] = dump
                _, notes = self.status_lines()
                self.assertTrue(any(n.startswith(expect) for n in notes), notes)
        del self.f.runner.pw_dump["gamer"]
        _, notes = self.status_lines()
        self.assertTrue(any("PipeWire did not answer" in n for n in notes), notes)

    def test_status_without_pw_dump_or_users(self):
        self.fake()
        self.apply()
        _, notes = self.status_lines()
        self.assertIn("nobody is logged in; PipeWire loads the profile at the next login", notes)
        self.f.login("gamer", 1000, pw_dump_json())
        self.path("/usr/bin/pw-dump").unlink()
        state, notes = self.status_lines()
        self.assertEqual(state, "applied")
        self.assertTrue(any("pw-dump is not installed" in n for n in notes), notes)

    def test_status_conflicts(self):
        self.fake()
        self.f.login("gamer", 1000, pw_dump_json())
        self.apply()
        self.f.write(f"/var/home/gamer/.config/pipewire/pipewire.conf.d/{templates.SPEAKER_PROFILE_NAME}", "x\n")
        self.f.write("/var/home/gamer/.config/pipewire/pipewire.conf.d/99-oxp-apex-speaker-dsp.conf",
                     "context.modules = [ { name = libpipewire-module-filter-chain args = { playback.props = { "
                     f'node.target = "{SPEAKER}" }} }} }} ]\n')
        self.f.write("/etc/pipewire/pipewire.conf.d/50-unrelated.conf", "context.properties = { }\n")
        _, notes = self.status_lines()
        warnings = [n for n in notes if n.startswith("warning:")]
        self.assertEqual(len(warnings), 2, notes)
        self.assertIn("replaces this profile for gamer", warnings[0])
        self.assertIn("99-oxp-apex-speaker-dsp.conf also puts a filter on the speakers", warnings[1])

    def test_note_suggests_it_when_volume_is_high(self):
        self.fake()
        self.f.login("gamer", 1000, pw_dump_json(filter_loaded=False, volume=3.375))
        state, notes = self.status_lines()
        self.assertEqual(state, "optional")
        self.assertTrue(any("speaker volume is 150%" in n and "louder without clipping" in n for n in notes), notes)
        self.f.runner.pw_dump["gamer"] = pw_dump_json(filter_loaded=False, volume=1.0)
        _, notes = self.status_lines()
        self.assertFalse(any("150%" in n for n in notes))

    def test_cli_status_and_list(self):
        self.fake()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["status"], system=self.f.system())
        self.assertIn("Sound fixes", out.getvalue())
        self.assertIn("speaker-profile", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["list", "--json"], system=self.f.system())
        fixes = {f["id"]: f for f in json.loads(out.getvalue())["catalog"]["fixes"]}
        self.assertEqual(fixes["speaker-profile"]["group"], "audio")
        self.assertEqual(fixes["speaker-profile"]["level"], "optional")
        self.assertNotIn("reboot", fixes["speaker-profile"]["takes_effect"])

    def test_cli_apply_dry_run(self):
        self.fake()
        self.f.login("gamer", 1000)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["apply", "speaker-profile", "--dry-run"], system=self.f.system())
        self.assertEqual(rc, 0)
        self.assertIn(f"create {CONF}", out.getvalue())
        self.assertIn("then restart gamer's PipeWire", out.getvalue())
        self.assertFalse(self.path(CONF).exists())


if __name__ == "__main__":
    unittest.main()
