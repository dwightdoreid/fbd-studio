"""Signal processing and math blocks, checked against theory in simulated time.

The signal comes from a SIG_GEN block in the same diagram (no AI quantisation);
PWM blocks are the probes. One sample is taken per scan cycle.
"""
import math

import pytest

from test_compiler import blk, wire
from test_control import Loop, lamp, loop, probe  # noqa: F401  (loop is a fixture)

PINS = ["GPIO25", "GPIO26", "GPIO27", "GPIO14", "GPIO13", "GPIO12"]


def gen(id_, name, wave="Sine", y=0, **values):
    return blk(id_, "SIG_GEN", name=name, y=y, values=values, params={"wave": wave})


def chain(source_blocks, block, outputs, wires=()):
    """The blocks and wires given, plus a probe (named after the output) on each of block's outputs."""
    blocks = list(source_blocks) + [block]
    wires = list(wires)
    for i, out in enumerate(outputs):
        if out in ("READY", "PULSE"):  # BOOL outputs go to a DO block
            blocks.append(lamp(f"p{i}", out, 100 * i, pin="GPIO33"))
        else:
            blocks.append(probe(f"p{i}", out, 100 * i, pin=PINS[i]))
        wires.append(wire(f"wp{i}", block.id, out, f"p{i}", "IN"))
    return blocks, wires


def amplitude(lp, name, t_settle, t_measure, **inputs):
    lp.run_until(t_settle, **inputs)
    vals = []
    while lp.t < t_settle + t_measure:
        vals.append(lp.step(**inputs)[name])
    return (max(vals) - min(vals)) / 2


# ------------------------------------------------------------------ generator

def test_signal_generator_waveforms(loop):
    for wave, at, expect in (("Sine", 0.25, 3.0), ("Square", 0.75, -3.0), ("Triangle", 0.25, 0.0),
                             ("Sawtooth", 0.75, 1.5)):
        b = blk("g", "SIG_GEN", values={"F": 1, "AMP": 3}, params={"wave": wave})
        lp = loop(*chain([], b, ["OUT"]), dt=0.001)
        assert lp.run_until(at)["OUT"] == pytest.approx(expect, abs=0.02), wave


def test_gaussian_noise_statistics(loop):
    b = blk("g", "SIG_GEN", values={"AMP": 2, "OFFSET": 5}, params={"wave": "Noise (Gaussian)"})
    lp = loop(*chain([], b, ["OUT"]))
    xs = [lp.step()["OUT"] for _ in range(20000)]
    mean = sum(xs) / len(xs)
    std = math.sqrt(sum((x - mean) ** 2 for x in xs) / len(xs))
    assert mean == pytest.approx(5, abs=0.05) and std == pytest.approx(2, rel=0.03)


# ------------------------------------------------------------------ filters

def filter_gain(loop, kind, order, f, fc=10.0, q=0.707, dt=0.001):
    blocks = [gen("g", "Src", F=f, AMP=1),
              blk("f", "FILTER", x=300, name="Filt", values={"FC": fc, "Q": q}, params={"kind": kind, "order": str(order)}),
              probe("p", "Out", 0)]
    wires = [wire("w1", "g", "OUT", "f", "IN"), wire("w2", "f", "OUT", "p", "IN")]
    lp = loop(blocks, wires, dt=dt)
    return amplitude(lp, "Out", 2.0, 1.0)


@pytest.mark.parametrize("kind, order, f, expect", [
    ("Low-pass", 2, 10, 1 / math.sqrt(2)),               # -3 dB at the cut-off
    ("Low-pass", 4, 10, 1 / math.sqrt(2)),
    ("Low-pass", 4, 20, 1 / math.sqrt(1 + 2 ** 8)),      # Butterworth: |H| = 1/sqrt(1 + (f/fc)^2n)
    ("Low-pass", 8, 20, 1 / math.sqrt(1 + 2 ** 16)),
    ("High-pass", 2, 10, 1 / math.sqrt(2)),
    ("High-pass", 2, 2.5, 1 / math.sqrt(1 + 4 ** 4)),
    ("Low-pass", 2, 1, 1.0),
])
def test_butterworth_gain(loop, kind, order, f, expect):
    # the bilinear transform warps frequencies a little; at f << fs the error is small
    assert filter_gain(loop, kind, order, f) == pytest.approx(expect, rel=0.03, abs=2e-4)


def test_notch_and_band_pass(loop):
    assert filter_gain(loop, "Notch (band-stop)", 2, 10, q=5) < 0.01
    assert filter_gain(loop, "Notch (band-stop)", 2, 3, q=5) > 0.98
    assert filter_gain(loop, "Band-pass", 2, 10, q=5) == pytest.approx(1, abs=0.01)
    assert filter_gain(loop, "Band-pass", 2, 3, q=5) < 0.1


def test_filter_starts_without_transient(loop):
    blocks = [blk("f", "FILTER", x=300, name="Filt", values={"IN": 7, "FC": 0.1}), probe("p", "Out", 0)]
    lp = loop(blocks, [wire("w", "f", "OUT", "p", "IN")])
    assert all(lp.step()["Out"] == pytest.approx(7, abs=1e-4) for _ in range(500))


def test_biquad_coefficients(loop):
    # one-pole low-pass y = 0.1 x + 0.9 y[-1] written as a biquad
    blocks = [gen("g", "Src", "Square", F=0.01, AMP=1, OFFSET=1),
              blk("b", "BIQUAD", x=300, name="Bq", values={"B0": 0.1, "A1": -0.9}), probe("p", "Out", 0)]
    lp = loop(blocks, [wire("w1", "g", "OUT", "b", "IN"), wire("w2", "b", "OUT", "p", "IN")])
    assert lp.step()["Out"] == pytest.approx(2)  # starts in steady state at the input (square is high first)
    # after the square wave drops to 0 at t = 50 s: y = 2 * 0.9^k
    lp.run_until(49.99)
    outs = [lp.step()["Out"] for _ in range(11)]
    assert outs[10] == pytest.approx(2 * 0.9 ** 10, rel=0.01)


def test_fir_moving_average(loop):
    blocks = [gen("g", "Src", "Square", F=0.5, AMP=1, OFFSET=1),
              blk("f", "FIR", x=300, name="Fir", values={f"B{i}": 0.25 for i in range(1, 5)}), probe("p", "Out", 0)]
    lp = loop(blocks, [wire("w1", "g", "OUT", "f", "IN"), wire("w2", "f", "OUT", "p", "IN")], dt=0.1)
    outs = [lp.step()["Out"] for _ in range(15)]
    assert outs[:5] == pytest.approx([2] * 5)          # high for 1 s = 10 samples
    assert outs[10:14] == pytest.approx([1.5, 1.0, 0.5, 0.0])


def test_median_removes_spikes(loop):
    # a 1-sample spike every 100 ms on top of a slow sine: a 10 Hz sawtooth is above 0.75 in one sample of ten
    blocks = [gen("g", "Slow", F=0.2, AMP=1), gen("s", "Spikes", "Sawtooth", y=200, F=10, AMP=1),
              blk("cmp", "GT", x=150, name="Narrow", values={"IN2": 0.75}, type_args={"T": "REAL"}),
              blk("sel", "SEL", x=250, name="Spike", values={"IN0": 0.0, "IN1": 50.0}, type_args={"T": "REAL"}),
              blk("add", "ADD", x=350, name="Sum", type_args={"T": "REAL"}),
              blk("m", "MEDIAN", x=450, name="Med", params={"n": 5}), probe("p", "Out", 0), probe("q", "Raw", 100, PINS[2])]
    wires = [wire("w1", "s", "OUT", "cmp", "IN1"), wire("w2", "cmp", "OUT", "sel", "G"), wire("w3", "g", "OUT", "add", "IN1"),
             wire("w4", "sel", "OUT", "add", "IN2"), wire("w5", "add", "OUT", "m", "IN"), wire("w6", "m", "OUT", "p", "IN"),
             wire("w7", "add", "OUT", "q", "IN")]
    lp = loop(blocks, wires, dt=0.01)
    outs = [lp.step() for _ in range(500)]
    assert max(o["Raw"] for o in outs) > 40  # the spikes are there ...
    assert max(o["Out"] for o in outs) < 1.01  # ... and gone after the median


# ------------------------------------------------------------------ analysis

@pytest.mark.parametrize("win", ["Hann", "Flat top", "Hamming"])
def test_fft_peak_band_and_dc(loop, win):
    blocks = [gen("g", "Tone", F=12.3, AMP=3, OFFSET=1), gen("h", "Hum", y=200, F=40, AMP=0.5),
              blk("a", "ADD", x=150, name="Sum", type_args={"T": "REAL"}),
              blk("f", "FFT", x=300, name="Spec", values={"F": 40, "F_LO": 5, "F_HI": 20}, params={"n": "256", "win": win})]
    outs = ["READY", "PEAK_F", "PEAK_A", "MAG", "BAND_RMS", "DC"]
    blocks, wires = chain(blocks, blocks.pop(), outs,
                          [wire("w1", "g", "OUT", "a", "IN1"), wire("w2", "h", "OUT", "a", "IN2"), wire("w3", "a", "OUT", "f", "IN")])
    lp = loop(blocks, wires, dt=0.005)  # 200 Hz
    frames = []
    for _ in range(256 * 3):
        o = lp.step()
        if o["READY"] > 0.5:
            frames.append(o)
    assert len(frames) == 3
    r = frames[-1]
    assert r["PEAK_F"] == pytest.approx(12.3, abs=0.01)  # between bins (DF = 0.78 Hz)
    assert r["PEAK_A"] == pytest.approx(3, rel=0.005)
    assert r["MAG"] == pytest.approx(0.5, rel=0.01)
    assert r["BAND_RMS"] == pytest.approx(3 / math.sqrt(2), rel=0.03)
    assert r["DC"] == pytest.approx(1, abs=0.05)


def test_fft_reports_sample_rate_and_resolution(loop):
    b = blk("f", "FFT", x=300, name="Spec", params={"n": "64"})
    blocks, wires = chain([gen("g", "Tone", F=5)], b, ["FS", "DF"], [wire("w1", "g", "OUT", "f", "IN")])
    lp = loop(blocks, wires, dt=0.01)
    o = lp.run_until(1.0)
    assert o["FS"] == pytest.approx(100, rel=1e-3) and o["DF"] == pytest.approx(100 / 64, rel=1e-3)


def test_goertzel_detects_one_frequency(loop):
    for f_sig, expect in ((50, 2.0), (30, 0.0)):
        blocks = [gen("g", "Tone", F=f_sig, AMP=2, OFFSET=3),
                  blk("d", "GOERTZEL", x=300, name="Det", values={"F": 50}, params={"n": 200})]
        blocks, wires = chain(blocks, blocks.pop(), ["MAG"], [wire("w1", "g", "OUT", "d", "IN")])
        lp = loop(blocks, wires, dt=0.001)
        assert lp.run_until(1.0)["MAG"] == pytest.approx(expect, abs=0.01), f_sig


def test_frequency_measurement(loop):
    blocks = [gen("g", "Tone", F=3.3, AMP=1), blk("m", "FREQ_MEAS", x=300, name="Freq")]
    blocks, wires = chain(blocks, blocks.pop(), ["F"], [wire("w1", "g", "OUT", "m", "IN")])
    lp = loop(blocks, wires, dt=0.01)
    assert lp.run_until(3.0)["F"] == pytest.approx(3.3, rel=0.003)


def test_rms_true_and_ac(loop):
    for mode, expect in (("AC + DC (true RMS)", math.sqrt(4 + 2)), ("AC only", math.sqrt(2))):
        blocks = [gen("g", "Tone", F=10, AMP=2, OFFSET=2), blk("r", "RMS", x=300, name="Rms", values={"T": "T#2s"},
                                                                params={"mode": mode})]
        blocks, wires = chain(blocks, blocks.pop(), ["OUT"], [wire("w1", "g", "OUT", "r", "IN")])
        lp = loop(blocks, wires, dt=0.001)
        assert lp.run_until(20.0)["OUT"] == pytest.approx(expect, rel=0.01), mode


def test_window_statistics(loop):
    b = blk("s", "STATS", x=300, name="St", params={"n": 100})
    blocks, wires = chain([gen("g", "Tone", "Triangle", F=1, AMP=2)], b, ["MEAN", "STD", "MIN", "MAX"],
                          [wire("w1", "g", "OUT", "s", "IN")])
    lp = loop(blocks, wires, dt=0.01)  # 100 samples = one period
    o = lp.run_until(3.0)
    assert o["MEAN"] == pytest.approx(0, abs=0.01)
    assert o["STD"] == pytest.approx(2 / math.sqrt(3), rel=0.01)  # triangle wave
    assert o["MIN"] == pytest.approx(-2, abs=0.05) and o["MAX"] == pytest.approx(2, abs=0.05)


def test_peak_hold_and_decay(loop):
    b = blk("k", "PEAK_HOLD", x=300, name="Pk", values={"HOLD": "T#1s", "DECAY": "T#500ms"}, params={"absolute": True})
    blocks, wires = chain([gen("g", "Burst", "Square", F=0.1, AMP=4)], b, ["OUT"], [wire("w1", "g", "OUT", "k", "IN")])
    lp = loop(blocks, wires, dt=0.01)
    assert lp.run_until(4.0)["OUT"] == pytest.approx(4)  # |+4|
    assert lp.run_until(5.5)["OUT"] == pytest.approx(4)  # |-4| keeps it at 4 too


def test_kalman_smooths_noise(loop):
    blocks = [gen("g", "Noisy", "Noise (Gaussian)", AMP=1, OFFSET=10),
              blk("k", "KALMAN", x=300, name="Kf", values={"Q": 0.001, "R": 1})]
    blocks, wires = chain(blocks, blocks.pop(), ["X"], [wire("w1", "g", "OUT", "k", "Z")])
    lp = loop(blocks, wires, dt=0.01)
    lp.run_until(20.0)
    xs = [lp.step()["X"] for _ in range(2000)]
    mean = sum(xs) / len(xs)
    std = math.sqrt(sum((x - mean) ** 2 for x in xs) / len(xs))
    assert mean == pytest.approx(10, abs=0.1)
    assert std < 0.1  # the measurement noise has a standard deviation of 1


def test_complementary_filter_follows_gyro_and_removes_drift(loop):
    # gyro reads a constant bias of 1 °/s while the true angle is 0: the accelerometer angle limits the drift
    blocks = [blk("c", "COMPL_FILTER", x=300, name="Cf", values={"RATE": 1, "ANGLE": 0, "T": "T#1s"}), probe("p", "Out", 0)]
    lp = loop(blocks, [wire("w", "c", "OUT", "p", "IN")], dt=0.01)
    assert lp.run_until(30.0)["Out"] == pytest.approx(1.0, abs=0.02)  # bias * T, instead of 30° of drift


def test_sample_and_hold(loop):
    blocks = [gen("g", "Ramp", "Sawtooth", F=0.1, AMP=10, OFFSET=10), gen("s", "Trig", "Square", y=200, F=1),
              blk("cmp", "GT", x=150, name="High", type_args={"T": "REAL"}),
              blk("h", "SAMPLE_HOLD", x=300, name="Sh")]
    blocks, wires = chain(blocks, blocks.pop(), ["OUT"],
                          [wire("w1", "g", "OUT", "h", "IN"), wire("w2", "s", "OUT", "cmp", "IN1"), wire("w3", "cmp", "OUT", "h", "S")])
    lp = loop(blocks, wires, dt=0.01)
    o1 = lp.run_until(0.5)["OUT"]
    o2 = lp.run_until(0.99)["OUT"]
    assert o1 == o2 == pytest.approx(0, abs=0.01)  # sampled at t = 0 and held
    assert lp.run_until(1.2)["OUT"] == pytest.approx(2, abs=0.05)  # sampled again at t = 1 s


# ------------------------------------------------------------------ math functions

@pytest.mark.parametrize("type_, values, expect", [
    ("SIN", {"IN": math.pi / 6}, 0.5), ("COS", {"IN": math.pi / 3}, 0.5), ("TAN", {"IN": math.pi / 4}, 1.0),
    ("ASIN", {"IN": 2.0}, math.pi / 2), ("ACOS", {"IN": 0.5}, math.pi / 3), ("ATAN", {"IN": 1.0}, math.pi / 4),
    ("ATAN2", {"Y": 1.0, "X": -1.0}, 3 * math.pi / 4), ("EXP", {"IN": 1.0}, math.e), ("LN", {"IN": math.e}, 1.0),
    ("LN", {"IN": -1.0}, 0.0), ("LOG", {"IN": 1000.0}, 3.0), ("EXPT", {"IN1": 2.0, "IN2": 10.0}, 1024.0),
    ("EXPT", {"IN1": -8.0, "IN2": 0.5}, 0.0),
])
def test_math_functions(loop, type_, values, expect):
    blocks = [blk("m", type_, x=300, name="M", values=values), probe("p", "Out", 0)]
    lp = loop(blocks, [wire("w", "m", "OUT", "p", "IN")])
    assert lp.step()["Out"] == pytest.approx(expect, rel=1e-5, abs=1e-6)


def test_deg_rad(loop):
    blocks = [blk("m", "DEG_RAD", x=300, name="M", values={"IN": 180}), probe("p", "Out", 0)]
    lp = loop(blocks, [wire("w", "m", "OUT", "p", "IN")])
    assert lp.step()["Out"] == pytest.approx(math.pi, rel=1e-6)


# ------------------------------------------------------------------ examples

def example_loop(loop, name, signals, params=None):
    """Runs an example with probes on the given internal signals ("Block.PORT")."""
    from test_modelica import load_example
    p = load_example(name)
    ids = {b.name: b.id for b in p.blocks}
    blocks, wires = list(p.blocks), list(p.wires)
    for i, sig in enumerate(signals):
        sb, sp = sig.split(".")
        blocks.append(probe(f"probe{i}", sig.replace(".", "_"), 900 + 100 * i, pin=["GPIO27", "GPIO14", "GPIO13", "GPIO12"][i]))
        wires.append(wire(f"wprobe{i}", ids[sb], sp, f"probe{i}", "IN"))
    return loop(blocks, wires, dt=p.settings.cycle_ms / 1000, params=params)


def test_filter_lab_example(loop):
    lp = example_loop(loop, "21 Filter lab", ["LowPass.OUT", "Spectrum.PEAK_F", "Spectrum.PEAK_A", "Spectrum.MAG"])
    lp.run_until(3.0)
    lows = [lp.step()["LowPass_OUT"] for _ in range(800)]  # 4 s
    assert 38.5 < min(lows) < 41 and 59 < max(lows) < 61.5  # the 40..60 signal, noise and hum gone
    o = lp.step()
    assert o["Spectrum_PEAK_F"] == pytest.approx(1, abs=0.05) and o["Spectrum_PEAK_A"] == pytest.approx(10, rel=0.03)
    assert o["Spectrum_MAG"] == pytest.approx(3, rel=0.05)  # the 50 Hz hum


def test_vibration_monitor_example(loop):
    lp = example_loop(loop, "22 Vibration monitor - FFT", ["Spectrum.PEAK_F", "Spectrum.BAND_RMS"])
    o = lp.run_until(5.0)
    assert o["Spectrum_PEAK_F"] == pytest.approx(24, abs=0.05)
    assert o["Spectrum_BAND_RMS"] == pytest.approx(0.4 / math.sqrt(2), rel=0.02)
    assert o["Alarm"] < 0.5
    lp = example_loop(loop, "22 Vibration monitor - FFT", ["Spectrum.BAND_RMS"], params={"TestVibration_AMP": 1.0})
    alarm_at = next(lp.t for _ in range(5000) if lp.step()["Alarm"] > 0.5)
    assert 2.5 <= alarm_at <= 3.1  # first FFT frame after 0.5 s, then 2 s TON


def test_tilt_sensor_example(loop):
    # the I2C readings are inputs here: the sensor is held at 35° and the gyro has a bias of 2 °/s
    lp = example_loop(loop, "23 Tilt sensor - MPU-6050", ["Tilt.OUT", "AccelDeg.OUT"])
    a = math.radians(35)
    raw = {"AccelY": round(16384 * math.sin(a)), "AccelZ": round(16384 * math.cos(a)), "GyroX": 2 * 131}
    o = lp.run_until(10.0, **raw)
    assert o["AccelDeg_OUT"] == pytest.approx(35, abs=0.01)
    assert o["Tilt_OUT"] == pytest.approx(37, abs=0.05)  # 35° + bias * T (1 s)
    assert o["TiltLamp"] > 0.5
