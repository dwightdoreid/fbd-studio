"""Modelica export: generated C API (always tested) and OpenModelica simulations (if omc is installed)."""
import ctypes
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from fbdstudio import builder, modelica
from fbdstudio.compiler import compile_project
from fbdstudio.project import Project

from test_compiler import blk, errors, wire

EXAMPLES = Path(__file__).parent.parent / "fbdstudio" / "examples" / "projects"


def load_example(name):
    return Project.model_validate(json.loads((EXAMPLES / f"{name}.fbd.json").read_text(encoding="utf-8")))


def compile_model(library, board, project):
    res = compile_project(project, library, board, target="model")
    assert res.ok, errors(res)
    return res


def shared_lib(res, pkg, tmp_path):
    """Build the generated C++ as a shared library (the export itself uses a static one)."""
    src = tmp_path / "prog.cpp"
    src.write_text(modelica.library_code(res, pkg))
    so = tmp_path / "prog.so"
    subprocess.run([builder.find_cxx(), "-std=c++11", "-shared", "-fPIC", "-fvisibility=hidden", "-w",
                    f"-I{modelica.MODEL_DIR}", f"-I{modelica.RUNTIME_DIR}", str(src), "-o", str(so)], check=True)
    lib = ctypes.CDLL(str(so))
    new = getattr(lib, f"fbd_{pkg}_new")
    new.restype = ctypes.c_void_p
    new.argtypes = [ctypes.c_double, ctypes.POINTER(ctypes.c_double), ctypes.c_int]
    step = getattr(lib, f"fbd_{pkg}_step")
    step.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.POINTER(ctypes.c_double), ctypes.c_int,
                     ctypes.POINTER(ctypes.c_double), ctypes.c_int]
    free = getattr(lib, f"fbd_{pkg}_free")
    free.argtypes = [ctypes.c_void_p]
    return new, step, free


def arr(values):
    return (ctypes.c_double * max(1, len(values)))(*values)


def timer_project():
    return Project(name="Timer test", blocks=[
        blk("di", "DI", name="Start", params={"pin": "GPIO4", "mode": "PULLUP", "invert": True}),
        blk("t", "TON", name="Delay", values={"PT": "T#2s"}),
        blk("do", "DO", name="Lamp", params={"pin": "GPIO25"}),
        blk("ai", "AI", name="Pot", y=100, params={"pin": "GPIO32"}),
        blk("pw", "PWM", name="Dim", y=100, params={"pin": "GPIO26"}),
    ], wires=[wire("w1", "di", "Q", "t", "IN"), wire("w2", "t", "Q", "do", "IN"), wire("w3", "ai", "PCT", "pw", "IN")])


def test_mapping_of_connectors_and_parameters(library, board):
    res = compile_model(library, board, timer_project())
    m = res.model
    assert [(q["name"], q["type"]) for q in m["inputs"]] == [("Start", "BOOL"), ("Pot", "REAL")]
    assert [(q["name"], q["type"]) for q in m["outputs"]] == [("Lamp", "BOOL"), ("Dim", "REAL")]
    assert [(q["name"], q["type"], q["value"]) for q in m["params"]] == [("Delay_PT", "TIME", 2.0)]
    mo = modelica.package_mo(timer_project(), res, "TimerTest")
    assert 'parameter Real Delay_PT(unit = "s") = 2.0' in mo
    assert "Modelica.Blocks.Interfaces.BooleanInput Start" in mo
    assert "fbdY := fbdStep(fbdProg, time, {(if Start then 1.0 else 0.0), Pot});" in mo
    assert "Lamp = fbdY[1] > 0.5;" in mo


def test_timers_run_in_simulated_time(library, board, tmp_path):
    res = compile_model(library, board, timer_project())
    new, step, free = shared_lib(res, "TimerTest", tmp_path)
    for preset, on_at in ((2.0, 2.0), (0.5, 0.5)):  # the parameter overrides the diagram's T#2s
        h = new(0.01, arr([preset]), 1)
        y = arr([0, 0])
        lamp = []
        for k in range(301):  # 3 s in 10 ms steps, button pressed from t = 0
            t = k * 0.01
            step(h, t, arr([1.0, 33.3]), 2, y, 2)
            lamp.append((round(t, 2), y[0]))
        free(h)
        first_on = next(t for t, v in lamp if v > 0.5)
        assert first_on == pytest.approx(on_at)
        assert y[1] == pytest.approx(33.3, abs=0.02)  # AI is quantised to 12 bit, PWM passes it on


def test_analog_input_is_clamped_and_quantised(library, board, tmp_path):
    res = compile_model(library, board, timer_project())
    new, step, free = shared_lib(res, "TimerTest", tmp_path)
    h = new(0.01, arr([2.0]), 1)
    y = arr([0, 0])
    for pct, expect in ((150.0, 100.0), (-5.0, 0.0), (50.0, round(2048 * 100 / 4095, 4))):
        step(h, 0.0, arr([0.0, pct]), 2, y, 2)
        assert y[1] == pytest.approx(expect, abs=1e-3)
    free(h)


def test_hardware_blocks_map_to_connectors(library, board):
    project = load_example("12 WiFi and MQTT")
    res = compile_model(library, board, project)
    names = {q["name"]: q["type"] for q in res.model["inputs"]}
    assert names == {"LightSensor": "REAL", "Door": "BOOL", "SubRelay": "BOOL", "SubDim": "REAL"}
    assert {q["name"] for q in res.model["outputs"]} == {"PubRssi", "PubLight", "PubDoor", "Relay", "LED"}
    multi = compile_model(library, board, load_example("09 Weather station")).model
    assert {q["name"] for q in multi["inputs"]} == {"Climate_T", "Climate_H", "Climate_P"}


def test_block_without_model_section_is_rejected(library, board):
    bdef = library.get("DS18B20").model_copy(update={"type": "MYSENSOR", "model": None})
    library.blocks["MYSENSOR"] = bdef
    try:
        res = compile_project(Project(name="x", blocks=[blk("s", "MYSENSOR", params={"pin": "GPIO4"})]),
                              library, board, target="model")
        assert any("cannot be used in a Modelica export" in m for m in errors(res))
    finally:
        del library.blocks["MYSENSOR"]


@pytest.mark.parametrize("name, pkg", [("03 Tank level", "TankLevel"), ("12 WiFi and MQTT", "WiFiAndMQTT"),
                                       ("Untitled", "Untitled"), ("model", "Model"), ("Modelica", "FBDModelica"), ("", "FBDProgram")])
def test_package_names(name, pkg):
    assert modelica.package_name(name) == pkg


def test_every_example_builds_a_library(library, boards, tmp_path):
    for f in sorted(EXAMPLES.glob("*.fbd.json")):
        project = Project.model_validate(json.loads(f.read_text(encoding="utf-8")))
        res = compile_model(library, boards.get(project.board), project)
        d = modelica.write_package(project, res, tmp_path)
        assert (d / "Resources" / "Library" / modelica.platform_dir() / f"libfbd_{d.name}.a").is_file()
        assert (d / "Resources" / "Source" / "build.sh").is_file()


# ---------------------------------------------------------------- OpenModelica

needs_omc = pytest.mark.skipif(shutil.which("omc") is None, reason="OpenModelica (omc) is not installed")


def run_omc(tmp_path, script):
    (tmp_path / "run.mos").write_text(script)
    proc = subprocess.run(["omc", "run.mos"], cwd=tmp_path, capture_output=True, text=True, timeout=900)
    return proc.stdout


@needs_omc
def test_tank_level_against_a_tank_model(library, board, tmp_path):
    """Software in the loop: the exported controller keeps a simulated tank between 20 and 80 %."""
    project = load_example("03 Tank level")
    modelica.write_package(project, compile_model(library, board, project), tmp_path)
    (tmp_path / "TankTest.mo").write_text("""
model TankTest
  Real level(start = 50, fixed = true) "% full";
  TankLevel.Controller ctrl;
  Blink.Controller blink "a second exported program in the same model";
equation
  ctrl.LevelSensor = level;
  der(level) = (if ctrl.PumpOut then 10 else 0) - 2;
end TankTest;
""")
    blink = load_example("01 Blink")
    modelica.write_package(blink, compile_model(library, board, blink), tmp_path)
    out = run_omc(tmp_path, """
loadModel(Modelica); loadFile("TankLevel/package.mo"); loadFile("Blink/package.mo"); loadFile("TankTest.mo");
r := simulate(TankTest, stopTime = 40, numberOfIntervals = 4000); getErrorString();
print("RESULT " + String(val(ctrl.PumpOut, 14.9)) + " " + String(val(ctrl.PumpOut, 15.2)) + " "
  + String(val(ctrl.PumpOut, 22.4)) + " " + String(val(ctrl.PumpOut, 22.7)) + " "
  + String(val(level, 40)) + " " + String(val(blink.LED, 0.25)) + " " + String(val(blink.LED, 0.75)) + "\\n");
""")
    line = next((ln for ln in out.splitlines() if ln.startswith("RESULT")), None)
    assert line, out[-3000:]
    p0, p1, p2, p3, level, b0, b1 = map(float, line.split()[1:])
    assert (p0, p1, p2, p3) == (0, 1, 1, 0)  # pump on below 20 % (t = 15 s), off above 80 % (t = 22.5 s)
    assert 18 < level < 82
    assert b0 != b1  # the blink program toggles every 0.5 s


@needs_omc
def test_every_example_package_simulates(library, boards, tmp_path):
    pkgs = []
    for f in sorted(EXAMPLES.glob("*.fbd.json")):
        project = Project.model_validate(json.loads(f.read_text(encoding="utf-8")))
        d = modelica.write_package(project, compile_model(library, boards.get(project.board), project), tmp_path)
        pkgs.append(d.name)
    script = "loadModel(Modelica);\n" + "".join(
        f'print("@@SIM {p}\\n"); loadFile("{p}/package.mo"); r := simulate({p}.Example, stopTime = 1); getErrorString();\n'
        for p in pkgs)
    out = run_omc(tmp_path, script)
    sections = {sec.split("\n", 1)[0].strip(): sec for sec in out.split("@@SIM ")[1:]}
    assert set(sections) == set(pkgs), out[-3000:]
    failed = {p: sec[-1500:] for p, sec in sections.items() if "The simulation finished successfully" not in sec}
    assert not failed, failed
