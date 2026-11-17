"""Test helpers for PipeWire config: SPA-JSON as PipeWire parses it, and a filter graph run sample by sample.

`parse_spa_json` follows spa_json_next() in spa/utils/json-core.h (PipeWire 1.4-1.6): whitespace, ',' ':' and '='
separate tokens, '#' starts a comment, bare words end at whitespace or '"' '#' ':' ',' '=' ']' '}', and a file may
be an object without its outer braces. It raises ValueError where PipeWire's parser reports an error.

`run_graph` runs a filter-chain `filter.graph` with the semantics of the builtin filter-graph plugin
(spa/plugins/filter-graph/plugin_builtin.c and audio-dsp-c.c, PipeWire 1.6.11; the same for these nodes in 1.4):
float32 samples, the RBJ biquads of spa/plugins/audioconvert/biquad.c in transposed direct form II, delays truncated
to whole samples at the graph rate, and unconnected inputs of max/mult left out.
"""

from __future__ import annotations

import math
import struct
from typing import Any

# Port names of the builtin labels the speaker profile uses, in both PipeWire 1.4.0 and 1.6.11 (1.4's max has only
# two inputs, 1.6's eight; 1.4's delay has no Feedback/Feedforward/latency).
_BQ = {"in": ["In"], "out": ["Out"], "control": ["Freq", "Q", "Gain", "b0", "b1", "b2", "a0", "a1", "a2"]}
BUILTIN_PORTS_1_4: dict[str, dict[str, list[str]]] = {
    "bq_lowpass": _BQ, "bq_highpass": _BQ, "bq_peaking": _BQ, "bq_highshelf": _BQ,
    "linear": {"in": ["In"], "out": ["Out"], "control": ["Control", "Mult", "Add"]},
    "abs": {"in": ["In"], "out": ["Out"], "control": []},
    "delay": {"in": ["In"], "out": ["Out"], "control": ["Delay (s)"], "config": ["max-delay"]},
    "mult": {"in": [f"In {i}" for i in range(1, 9)], "out": ["Out"], "control": []},
    "max": {"in": ["In 1", "In 2"], "out": ["Out"], "control": []},
    "clamp": {"in": ["In"], "out": ["Out"], "control": ["Control", "Min", "Max"]},
    "sine": {"in": [], "out": ["Out"], "control": ["Freq", "Ampl", "Phase", "Offset"]},
    "recip": {"in": ["In"], "out": ["Out"], "control": ["Control"]},
}

_BARE_END = set('\t\r\n "#:,=]}\0')


def parse_spa_json(text: str) -> Any:
    pos = 0
    n = len(text)

    def skip() -> None:
        nonlocal pos
        while pos < n:
            c = text[pos]
            if c in " \t\r\n,\0":
                pos += 1
            elif c == "#":
                while pos < n and text[pos] != "\n":
                    pos += 1
            else:
                return

    def token() -> Any:
        nonlocal pos
        skip()
        if pos >= n:
            raise ValueError("unexpected end of input")
        c = text[pos]
        if c == "{":
            pos += 1
            return obj("}")
        if c == "[":
            pos += 1
            return arr()
        if c in "}]":
            raise ValueError(f"unexpected {c!r} at {pos}")
        if c in ":=":
            raise ValueError(f"unexpected {c!r} at {pos}")
        if c == '"':
            pos += 1
            out = []
            while True:
                if pos >= n:
                    raise ValueError("unfinished string")
                c = text[pos]
                pos += 1
                if c == '"':
                    return "".join(out)
                if c == "\\":
                    if pos >= n:
                        raise ValueError("unfinished string")
                    e = text[pos]
                    pos += 1
                    if e not in '"\\/bfnrtu':
                        raise ValueError(f"invalid escape \\{e}")
                    out.append({"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}.get(e, e))
                    continue
                if ord(c) < 0x20:
                    raise ValueError("control character in a string")
                out.append(c)
        if c == "\\":
            raise ValueError("bare escape")
        start = pos
        while pos < n and text[pos] not in _BARE_END:
            if not 32 <= ord(text[pos]) <= 126:
                raise ValueError(f"character {text[pos]!r} not allowed in a bare word")
            pos += 1
        return Bare(text[start:pos])

    def sep() -> None:
        nonlocal pos
        skip()
        if pos < n and text[pos] in ":=":
            pos += 1

    def obj(close: str) -> dict:
        nonlocal pos
        out: dict = {}
        while True:
            skip()
            if pos >= n:
                if close:
                    raise ValueError("expected object close")
                return out
            if text[pos] == close:
                pos += 1
                return out
            key = token()
            if not isinstance(key, str):
                raise ValueError(f"expected an object key at {pos}")
            sep()
            skip()
            if pos >= n or text[pos] in "}]":
                raise ValueError(f"expected a value for {key!r}")
            out[str(key)] = token()

    def arr() -> list:
        nonlocal pos
        out = []
        while True:
            skip()
            if pos >= n:
                raise ValueError("expected array close")
            if text[pos] == "]":
                pos += 1
                return out
            if text[pos] in ":=":
                raise ValueError("invalid array separator")
            out.append(token())

    skip()
    if pos < n and text[pos] == "{":
        pos += 1
        result = obj("}")
        skip()
        if pos < n:
            raise ValueError(f"trailing data at {pos}")
        return result
    return obj("")


class Bare(str):
    """A bare word (number, bool or unquoted string), kept as written."""


def f32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", x))[0]


def _number(v: Any) -> float:
    return f32(float(v))


def _biquad_coeffs(label: str, rate: int, freq: float, q: float, gain: float) -> tuple[float, ...]:
    w0 = math.pi * (freq * 2 / rate)
    alpha = math.sin(w0) / (2 * q)
    k = math.cos(w0)
    a_ = 10 ** (gain / 40)
    if label == "bq_lowpass":
        b, a = ((1 - k) / 2, 1 - k, (1 - k) / 2), (1 + alpha, -2 * k, 1 - alpha)
    elif label == "bq_highpass":
        b, a = ((1 + k) / 2, -(1 + k), (1 + k) / 2), (1 + alpha, -2 * k, 1 - alpha)
    elif label == "bq_peaking":
        b, a = (1 + alpha * a_, -2 * k, 1 - alpha * a_), (1 + alpha / a_, -2 * k, 1 - alpha / a_)
    elif label == "bq_highshelf":
        k2 = 2 * math.sqrt(a_) * alpha
        b = (a_ * ((a_ + 1) + (a_ - 1) * k + k2), -2 * a_ * ((a_ - 1) + (a_ + 1) * k),
             a_ * ((a_ + 1) + (a_ - 1) * k - k2))
        a = ((a_ + 1) - (a_ - 1) * k + k2, 2 * ((a_ - 1) - (a_ + 1) * k), (a_ + 1) - (a_ - 1) * k - k2)
    else:
        raise ValueError(label)
    return tuple(f32(v / a[0]) for v in (*b, a[1], a[2]))


def biquad_response_db(label: str, rate: int, freq: float, q: float, gain: float, at: float) -> float:
    b0, b1, b2, a1, a2 = _biquad_coeffs(label, rate, freq, q, gain)
    z = complex(math.cos(2 * math.pi * at / rate), math.sin(2 * math.pi * at / rate))
    h = (b0 + b1 / z + b2 / z ** 2) / (1 + a1 / z + a2 / z ** 2)
    return 20 * math.log10(abs(h))


def _biquad(x: list[float], c: tuple[float, ...]) -> list[float]:
    b0, b1, b2, a1, a2 = c
    x1 = x2 = 0.0
    out = []
    for v in x:
        y = b0 * v + x1
        x1 = b1 * v - a1 * y + x2
        x2 = b2 * v - a2 * y
        out.append(y)
    return out


def delay_samples(node: dict, rate: int) -> int:
    """What PipeWire's builtin delay turns the node's "Delay (s)" into at ``rate`` (clamped to its buffer)."""
    max_delay = _number(node.get("config", {}).get("max-delay", 1.0))
    buffer = -(-int(f32(max_delay * rate)) // 64) * 64
    return min(int(f32(_number(node.get("control", {}).get("Delay (s)", 0.0)) * rate)), buffer - 1)


def run_graph(graph: dict, inputs: list[list[float]], rate: int) -> tuple[list[list[float]], dict[str, list[float]]]:
    """Run ``graph`` (a parsed filter.graph) on one block per graph input; returns its outputs and every node's Out."""
    nodes = {n["name"]: n for n in graph["nodes"]}
    feeds: dict[str, dict[str, str]] = {}
    for link in graph["links"]:
        node, port = link["input"].split(":", 1)
        feeds.setdefault(node, {})[port] = link["output"].split(":", 1)[0]
    graph_in = {p.split(":", 1)[0]: i for i, p in enumerate(graph["inputs"])}
    length = len(inputs[0])
    done: dict[str, list[float]] = {}

    def out(name: str) -> list[float]:
        if name in done:
            return done[name]
        n = nodes[name]
        label = n["label"]
        ctl = {k: _number(v) for k, v in n.get("control", {}).items()}
        ins = feeds.get(name, {})
        x = inputs[graph_in[name]] if name in graph_in else (out(ins["In"]) if "In" in ins else None)
        if label.startswith("bq_"):
            y = _biquad(x, _biquad_coeffs(label, rate, ctl["Freq"], ctl["Q"], ctl.get("Gain", 0.0)))
        elif label == "linear":
            y = [v * ctl.get("Mult", 1.0) + ctl.get("Add", 0.0) for v in x]
        elif label == "abs":
            y = [abs(v) for v in x]
        elif label == "delay":
            d = delay_samples(n, rate)
            y = ([0.0] * d + x[:length - d]) if d else list(x)
        elif label in ("max", "mult"):
            srcs = [out(ins[p]) for p in sorted(ins) if p.startswith("In ")]
            y = list(srcs[0]) if srcs else [0.0] * length
            for s in srcs[1:]:
                pairs = zip(y, s, strict=True)
                y = [max(a, b) for a, b in pairs] if label == "max" else [a * b for a, b in pairs]
        elif label == "clamp":
            lo, hi = ctl.get("Min", 0.0), ctl.get("Max", 1.0)
            y = [min(max(v, lo), hi) for v in x]
        elif label == "recip":
            y = [0.0 if x[0] == 0.0 else 1.0 / v for v in x]
        elif label == "sine":
            if ctl.get("Ampl", 1.0) != 0.0:
                raise ValueError("only a constant sine (Ampl 0) is simulated")
            y = [ctl.get("Offset", 0.0)] * length
        else:
            raise ValueError(f"no simulation for {label}")
        done[name] = y
        return y

    outs = [out(p.split(":", 1)[0]) for p in graph["outputs"]]
    for name in nodes:
        out(name)
    return outs, done
