"""Signal capture (scope) through the monitor protocol, against the real simulator process."""
import math

import pytest

from fbdstudio import builder
from fbdstudio.compiler import compile_project
from fbdstudio.link import SimLink, parse_line
from fbdstudio.project import Project

from test_compiler import blk, errors, sim_events, wait_for, wire  # noqa: F401  (sim_events is a fixture)


@pytest.fixture
def sim(library, board, tmp_path, sim_events):
    """A running simulator with a 5 Hz sine (SIG_GEN), its square wave and a counter, scan cycle 2 ms."""
    p = Project(settings={"cycle_ms": 2, "monitor_ms": 0}, blocks=[
        blk("g", "SIG_GEN", name="Sine", values={"F": 5, "AMP": 2}),
        blk("cmp", "GT", x=200, name="Pos", type_args={"T": "REAL"}),
        blk("do", "DO", x=400, name="Lamp", params={"pin": "GPIO16"}),
        blk("bl", "BLINK", y=200, name="Blink", values={"T_ON": "T#100ms", "T_OFF": "T#100ms"}),
    ], wires=[wire("w1", "g", "OUT", "cmp", "IN1"), wire("w2", "cmp", "OUT", "do", "IN")])
    res = compile_project(p, library, board, target="sim")
    assert res.ok, errors(res)
    exe = builder.build_sim(res, tmp_path)
    sym = {f"{s['block']}.{s['port']}": s["idx"] for s in res.symbols}
    link = SimLink(exe, res.symbols, res.runtime_hash, sim_events.put)
    wait_for(sim_events, lambda e: e["ev"] == "hello")
    yield link, sym, sim_events
    link.close()


def test_immediate_capture(sim):
    link, sym, q = sim
    link.start_capture([sym["g.OUT"], sym["cmp.OUT"]], length=300)
    assert wait_for(q, lambda e: e["ev"] == "capture_state")["state"] == "armed"
    cap = wait_for(q, lambda e: e["ev"] == "capture", timeout=10)
    sine, pos = cap["data"]
    assert cap["channels"] == [sym["g.OUT"], sym["cmp.OUT"]] and cap["pre"] == 0
    assert len(sine) == len(pos) == 300
    assert cap["period_us"] == pytest.approx(2000, rel=0.1)  # one sample per 2 ms scan
    assert max(sine) == pytest.approx(2, abs=0.05) and min(sine) == pytest.approx(-2, abs=0.05)
    assert all(p == (1.0 if s > 0 else 0.0) for s, p in zip(sine, pos) if abs(s) > 0.01)  # same scan, consistent


def test_triggered_capture_with_pre_trigger(sim):
    link, sym, q = sim
    link.start_capture([sym["g.OUT"]], length=200, pre=50, trigger=sym["g.OUT"], edge=1, level=1.0)
    assert wait_for(q, lambda e: e["ev"] == "capture_state" and e["state"] == "triggered", timeout=5)
    cap = wait_for(q, lambda e: e["ev"] == "capture", timeout=10)
    (sine,) = cap["data"]
    assert cap["pre"] == 50
    assert sine[49] < 1.0 <= sine[50]  # the trigger sample is at index pre
    assert all(abs(math.asin(max(-1, min(1, v / 2)))) <= math.pi / 2 for v in sine)


def test_decimated_capture_and_falling_edge_of_bool(sim):
    link, sym, q = sim
    link.start_capture([sym["bl.Q"]], every=5, length=100, pre=10, trigger=sym["bl.Q"], edge=2, level=0.5)
    cap = wait_for(q, lambda e: e["ev"] == "capture", timeout=10)
    (blink,) = cap["data"]
    assert cap["period_us"] == pytest.approx(10000, rel=0.1)  # every 5th scan of 2 ms
    assert blink[9] == 1.0 and blink[10] == 0.0  # falling edge at the trigger position
    runs = sum(1 for a, b in zip(blink, blink[1:]) if a != b)
    assert 8 <= runs <= 11  # 100 ms on / 100 ms off over 1 s


def test_cancel_and_bad_requests(sim):
    link, sym, q = sim
    link.start_capture([sym["g.OUT"]], length=100, trigger=sym["cmp.OUT"], level=5.0)  # never triggers
    wait_for(q, lambda e: e["ev"] == "capture_state")
    link.cancel_capture()
    with pytest.raises(ValueError):
        link.start_capture([], length=100)
    with pytest.raises(ValueError):
        link.start_capture([999])
    # a capture right after the cancel works
    link.start_capture([sym["g.OUT"]], length=50)
    assert len(wait_for(q, lambda e: e["ev"] == "capture", timeout=10)["data"][0]) == 50


def test_long_capture_is_limited_by_the_buffer(sim):
    link, sym, q = sim
    link.start_capture([sym["g.OUT"], sym["cmp.OUT"], sym["bl.Q"], sym["bl.EN"]], length=60000)
    st = wait_for(q, lambda e: e["ev"] == "capture_state")
    assert st["len"] == 4096 // 4  # PLC_CAPTURE_MAX values shared by the channels


def test_capture_lines_do_not_disturb_the_value_parser():
    assert parse_line("%D 1 2", [])["ev"] == "serial"  # only the Link handles capture lines
