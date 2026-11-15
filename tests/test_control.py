"""Control and filter blocks, checked against analytic responses in simulated time.

Each test builds a small diagram and runs it through the Modelica export's C API
(shared library + ctypes), so the scan cycle and time are exact. AI blocks feed
values in (in %, 12-bit quantised), PWM and DO blocks are the probes.
"""
import math

import pytest

from fbdstudio.project import Project

from test_compiler import blk, wire
from test_modelica import arr, compile_model, shared_lib


class Loop:
    """Runs a diagram with a fixed scan cycle; inputs and outputs by block name."""

    def __init__(self, library, board, tmp_path, blocks, wires, dt=0.01, params=None):
        res = compile_model(library, board, Project(name="Ctl", blocks=blocks, wires=wires))
        self.ins = [q["name"] for q in res.model["inputs"]]
        self.outs = [q["name"] for q in res.model["outputs"]]
        new, self._step, self._free = shared_lib(res, "Ctl", tmp_path)
        overrides = dict(params or {})  # e.g. {"Pid_KP": 2.0}: like changing a value online
        params = [overrides.pop(q["name"], q["value"]) for q in res.model["params"]]
        assert not overrides, f"unknown parameters {list(overrides)}"
        self.h = new(dt, arr(params), len(params))
        self.dt, self.k, self.y = dt, 0, arr([0] * len(self.outs))

    @property
    def t(self):
        return self.k * self.dt

    def step(self, **inputs):
        u = arr([float(inputs.get(n, 0.0)) for n in self.ins])
        self._step(self.h, self.t, u, len(self.ins), self.y, len(self.outs))
        self.k += 1
        return {n: self.y[i] for i, n in enumerate(self.outs)}

    def run_until(self, t_end, **inputs):
        out = None
        while self.t <= t_end + 1e-9:
            out = self.step(**inputs)
        return out

    def close(self):
        self._free(self.h)


@pytest.fixture
def loop(library, board, tmp_path):
    made = []

    def make(blocks, wires, dt=0.01, params=None):
        d = tmp_path / str(len(made))
        d.mkdir()
        lp = Loop(library, board, d, blocks, wires, dt, params)
        made.append(lp)
        return lp

    yield make
    for lp in made:
        lp.close()


def ai(id_, name, y):
    return blk(id_, "AI", name=name, y=y, params={"pin": "GPIO32" if y == 0 else "GPIO33"})


def probe(id_, name, y, pin="GPIO26"):
    return blk(id_, "PWM", name=name, x=600, y=y, params={"pin": pin})


def lamp(id_, name, y, pin="GPIO25"):
    return blk(id_, "DO", name=name, x=600, y=y, params={"pin": pin})


def single(type_, values=None, params=None, src=None, dst="OUT"):
    """AI 'In' -> block input src (optional) ; block output dst -> PWM 'Out'."""
    blocks = [blk("b", type_, x=300, name="B", values=values or {}, params=params or {}), probe("o", "Out", 0)]
    wires = [wire("w2", "b", dst, "o", "IN")]
    if src:
        blocks.append(ai("i", "In", 0))
        wires.append(wire("w1", "i", "PCT", "b", src))
    return blocks, wires


def test_pt1_step_response(loop):
    lp = loop(*single("PT1", {"IN": 10, "K": 2, "T": "T#1s"}, {"start_at_input": False}))
    assert lp.run_until(1.0)["Out"] == pytest.approx(20 * (1 - math.exp(-1)), rel=1e-4)
    assert lp.run_until(10.0)["Out"] == pytest.approx(20, rel=1e-3)


def test_pt1_starts_at_input_by_default(loop):
    lp = loop(*single("PT1", {"IN": 10, "T": "T#5s"}))
    assert lp.step()["Out"] == pytest.approx(10)


def test_pt2_critically_damped_step(loop):
    lp = loop(*single("PT2", {"IN": 1, "T": "T#1s", "D": 1.0}, {"start_at_input": False}), dt=0.001)
    assert lp.run_until(1.0)["Out"] == pytest.approx(1 - 2 * math.exp(-1), abs=1e-3)
    assert lp.run_until(3.0)["Out"] == pytest.approx(1 - 4 * math.exp(-3), abs=1e-3)


def test_pt2_underdamped_overshoot(loop):
    d = 0.2
    lp = loop(*single("PT2", {"IN": 1, "T": "T#100ms", "D": d}, {"start_at_input": False}), dt=0.001)
    peak = max(lp.step()["Out"] for _ in range(2000))
    assert peak - 1 == pytest.approx(math.exp(-math.pi * d / math.sqrt(1 - d * d)), abs=5e-3)


def test_lead_lag_jumps_then_settles(loop):
    lp = loop(*single("LEAD_LAG", {"K": 1, "T1": "T#1s", "T2": "T#100ms"}, src="IN"), dt=0.001)
    assert lp.run_until(0.5, In=0)["Out"] == pytest.approx(0, abs=1e-6)
    jump = lp.step(In=5)["Out"]
    assert jump == pytest.approx(50, rel=0.02)  # K * T1/T2 * step at the first instant
    assert lp.run_until(0.6, In=5)["Out"] == pytest.approx(5 + 45 * math.exp(-1), rel=0.02)
    assert lp.run_until(2.0, In=5)["Out"] == pytest.approx(5, abs=0.01)  # 5 % is 5.006 after the 12-bit AI


def test_integrator_with_limit_and_reset(loop):
    blocks, wires = single("INTEG", {"IN": 2, "K": 1, "OUT_MAX": 7})
    blocks.append(blk("di", "DI", name="Reset", params={"pin": "GPIO4"}))
    wires.append(wire("w3", "di", "Q", "b", "RST"))
    lp = loop(blocks, wires)
    assert lp.run_until(3.0)["Out"] == pytest.approx(6, abs=1e-3)
    assert lp.run_until(5.0)["Out"] == pytest.approx(7)
    assert lp.step(Reset=True)["Out"] == pytest.approx(0)


def test_differentiator_of_a_ramp(loop):
    lp = loop(*single("DT1", {"K": 1, "T": "T#50ms"}, src="IN"))
    out = None
    while lp.t < 2:
        out = lp.step(In=10 * lp.t)  # 10 %/s
    assert out["Out"] == pytest.approx(10, rel=0.02)


def test_dead_time(loop):
    lp = loop(*single("DEADTIME", {"T": "T#500ms"}, src="IN"))
    lp.run_until(1.0, In=0)
    times = []
    while lp.t < 2:
        t = lp.t
        if lp.step(In=40)["Out"] > 20:
            times.append(t)
    assert 1.49 <= times[0] <= 1.53


def test_ramp_limits_rate(loop):
    lp = loop(*single("RAMP", {"UP": 10, "DOWN": 20}, src="IN"))
    lp.run_until(0.5, In=0)
    t0 = lp.t
    assert lp.run_until(t0 + 2.0, In=50)["Out"] == pytest.approx(20, abs=0.15)
    assert lp.run_until(t0 + 6.0, In=50)["Out"] == pytest.approx(50, abs=0.02)
    t1 = lp.t
    assert lp.run_until(t1 + 1.0, In=0)["Out"] == pytest.approx(30, abs=0.25)


def test_moving_average(loop):
    lp = loop(*single("MOV_AVG", params={"n": 4}, src="IN"))
    outs = [lp.step(In=v)["Out"] for v in (40, 40, 40, 40, 80, 80, 80, 80, 80)]
    assert outs[3] == pytest.approx(40, abs=0.02)
    assert outs[4] == pytest.approx(50, abs=0.02)
    assert outs[7] == pytest.approx(80, abs=0.02)


def test_deadband(loop):
    lp = loop(*single("DEADBAND", {"DB": 10}, src="IN"))
    assert lp.step(In=5)["Out"] == pytest.approx(0)
    assert lp.step(In=30)["Out"] == pytest.approx(20, abs=0.02)


def test_onoff_hysteresis(loop):
    blocks = [ai("i", "Temp", 0), blk("c", "ONOFF", x=300, name="C", values={"SP": 50, "HYS": 4}), lamp("q", "Heater", 0)]
    wires = [wire("w1", "i", "PCT", "c", "PV"), wire("w2", "c", "Q", "q", "IN")]
    lp = loop(blocks, wires)
    seq = [(45, 1), (49, 1), (51, 1), (52.5, 0), (50, 0), (48.5, 0), (47.5, 1)]
    assert [round(lp.step(Temp=v)["Heater"]) for v, _ in seq] == [q for _, q in seq]


def test_time_proportioning_duty(loop):
    blocks = [blk("t", "TPO", x=300, name="T", values={"IN": 30, "PERIOD": "T#1s"}), lamp("q", "Relay", 0)]
    lp = loop(blocks, [wire("w", "t", "Q", "q", "IN")], dt=0.001)
    on = sum(lp.step()["Relay"] > 0.5 for _ in range(3000))
    assert on == pytest.approx(900, abs=3)


def test_tpo_min_pulse(loop):
    blocks = [blk("t", "TPO", x=300, name="T", values={"IN": 5, "PERIOD": "T#1s", "MIN_PULSE": "T#100ms"}),
              lamp("q", "Relay", 0)]
    lp = loop(blocks, [wire("w", "t", "Q", "q", "IN")], dt=0.001)
    assert sum(lp.step()["Relay"] > 0.5 for _ in range(2000)) == 0


def test_pid_proportional_and_manual(loop):
    blocks = [ai("i", "Meas", 0), blk("p", "PID", x=300, name="P", values={"SP": 50, "KP": 2, "TI": "T#0s"}),
              probe("o", "Out", 0), blk("m", "DI", name="Manual", y=200, params={"pin": "GPIO4"})]
    wires = [wire("w1", "i", "PCT", "p", "PV"), wire("w2", "p", "OUT", "o", "IN"), wire("w3", "m", "Q", "p", "MAN")]
    lp = loop(blocks, wires)
    assert lp.step(Meas=40)["Out"] == pytest.approx(20, abs=0.05)
    assert lp.step(Meas=10)["Out"] == pytest.approx(80, abs=0.05)
    assert lp.step(Meas=0)["Out"] == pytest.approx(100)  # limited to OUT_MAX
    assert lp.step(Meas=40, Manual=True)["Out"] == pytest.approx(0)  # MAN_OUT = 0
    assert lp.step(Meas=40)["Out"] == pytest.approx(0, abs=0.05)  # bumpless: no jump back to 20


def pid_with_plant(values, params=None):
    """PID -> PT1 plant (K = 1, T = 2 s) -> back to PV (a feedback wire)."""
    blocks = [blk("p", "PID", x=0, name="Ctrl", values={"SP": 50, **values}, params=params or {}),
              blk("g", "PT1", x=300, name="Plant", values={"K": 1, "T": "T#2s"}, params={"start_at_input": False}),
              probe("o", "PV", 0), probe("u", "U", 100, pin="GPIO27")]
    wires = [wire("w1", "p", "OUT", "g", "IN"), wire("w2", "g", "OUT", "p", "PV"),
             wire("w3", "g", "OUT", "o", "IN"), wire("w4", "p", "OUT", "u", "IN")]
    return blocks, wires


def test_pid_closed_loop_has_no_steady_state_error(loop):
    lp = loop(*pid_with_plant({"KP": 2, "TI": "T#2s"}))
    assert lp.run_until(5.0)["PV"] > 40
    assert lp.run_until(30.0)["PV"] == pytest.approx(50, abs=0.05)


def test_pid_anti_windup(loop):
    # SP = 80 cannot be reached with OUT_MAX = 60, so the output sits at the limit for 20 s.
    # When SP drops to 30 the output must leave the limit at once instead of unwinding the integral.
    blocks, wires = pid_with_plant({"KP": 1, "TI": "T#1s", "OUT_MAX": 60})
    blocks.append(ai("i", "Sp", 0))
    wires.append(wire("w5", "i", "PCT", "p", "SP"))
    lp = loop(blocks, wires)
    out = lp.run_until(20.0, Sp=80)
    assert out["U"] == pytest.approx(60) and out["PV"] == pytest.approx(60, abs=0.1)
    assert lp.step(Sp=30)["U"] < 20
    assert lp.run_until(40.0, Sp=30)["PV"] == pytest.approx(30, abs=0.05)


def test_pid_reverse_action(loop):
    lp = loop(*single("PID", {"SP": 50, "PV": 60, "KP": 1, "TI": "T#0s"}, {"action": "e = PV - SP (cooling, draining)"}))
    assert lp.step()["Out"] == pytest.approx(10)


def test_pid_derivative_on_error_and_on_pv(loop):
    for d_on_pv, expect in ((False, True), (True, False)):
        blocks = [ai("i", "Sp", 0), blk("p", "PID", x=300, name="P", values={"PV": 0, "KP": 1, "TI": "T#0s",
                  "TD": "T#1s", "N": 10, "OUT_MIN": -1000, "OUT_MAX": 1000}, params={"d_on_pv": d_on_pv}),
                  probe("o", "Out", 0)]
        lp = loop(blocks, [wire("w1", "i", "PCT", "p", "SP"), wire("w2", "p", "OUT", "o", "IN")])
        lp.run_until(0.5, Sp=0)
        kick = lp.step(Sp=10)["Out"]
        assert (kick > 50) is expect, kick  # a setpoint step kicks the D part only with derivative on error


def test_state_feedback_double_integrator(loop):
    """LQR-style regulation of a double integrator x1'' = u with u = KR*r - k1*x1 - k2*x2."""
    blocks = [blk("c", "STATE_FB", x=0, name="Ctl", values={"R": 1, "K1": 1, "K2": 1.732, "KR": 1}),
              blk("v", "INTEG", x=300, y=0, name="Vel"), blk("x", "INTEG", x=500, y=0, name="Pos"),
              probe("o", "X1", 0)]
    wires = [wire("w1", "c", "OUT", "v", "IN"), wire("w2", "v", "OUT", "x", "IN"),
             wire("w3", "x", "OUT", "c", "X1"), wire("w4", "v", "OUT", "c", "X2"), wire("w5", "x", "OUT", "o", "IN")]
    lp = loop(blocks, wires, dt=0.001)
    assert lp.run_until(10.0)["X1"] == pytest.approx(1, abs=0.01)


def test_state_feedback_integral_action(loop):
    # a constant disturbance of -0.5 on the input: only the integral part (LQI) removes the offset
    blocks = [blk("c", "STATE_FB", x=0, name="Ctl", values={"R": 1, "K1": 2, "KR": 2, "KI": 1}, counts={"X": 1, "K": 1}),
              blk("a", "ADD", x=200, name="Dist", values={"IN2": -0.5}, type_args={"T": "REAL"}),
              blk("g", "PT1", x=400, name="Plant", values={"K": 1, "T": "T#1s"}, params={"start_at_input": False}),
              probe("o", "Y", 0)]
    wires = [wire("w1", "c", "OUT", "a", "IN1"), wire("w2", "a", "OUT", "g", "IN"),
             wire("w3", "g", "OUT", "c", "X1"), wire("w4", "g", "OUT", "o", "IN")]
    lp = loop(blocks, wires)
    assert lp.run_until(30.0)["Y"] == pytest.approx(1, abs=0.01)


@pytest.mark.parametrize("example, src, target, t_end, max_peak", [
    ("19 PID tuning with a simulated plant", "Pipe.OUT", 50.0, 150.0, 55.0),
    ("20 State feedback - motor position", "MotorAngle.OUT", 1.0, 3.0, 1.03),
])
def test_control_examples_settle(loop, example, src, target, t_end, max_peak):
    from test_modelica import load_example
    p = load_example(example)
    ids = {b.name: b.id for b in p.blocks}
    sb, sp = src.split(".")
    blocks = list(p.blocks) + [probe("probe", "Y", 900, pin="GPIO27")]
    wires = list(p.wires) + [wire("wprobe", ids[sb], sp, "probe", "IN")]
    lp = loop(blocks, wires, dt=p.settings.cycle_ms / 1000)
    peak = 0
    while lp.t < t_end:
        peak = max(peak, lp.step()["Y"])
    assert lp.step()["Y"] == pytest.approx(target, rel=0.01)
    assert peak < max_peak
