"""Export a diagram as a Modelica package for software-in-the-loop tests (e.g. OpenModelica).

The program is compiled with target "model" (see compiler._generate_model) into a static
C++ library with a small C API. The package wraps it in an external object and runs one
scan per sample period:

    <Pkg>/package.mo                         Controller block + Example model
    <Pkg>/Resources/Library/<platform>/      libfbd_<Pkg>.a (found via annotation(Library=...))
    <Pkg>/Resources/Source/                  the C++ source and a build script for other systems

Mapping (defined per block in the YAML 'model:' sections):
    DI -> BooleanInput, AI -> RealInput (% of full scale), DO -> BooleanOutput,
    PWM -> RealOutput (%), sensor values -> RealInput, MQTT/BT values -> inputs/outputs;
    unconnected inputs of logic blocks (setpoints, timer presets, ...) -> parameters.
IEC types: BOOL -> Boolean, INT/DINT -> Integer, REAL -> Real, TIME -> Real in seconds.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from .builder import RUNTIME_DIR, find_cxx
from .compiler import CompileResult
from .project import Project

MODEL_DIR = RUNTIME_DIR / "model"
MSL_VERSION = "4.0.0"

MODELICA_KEYWORDS = set(
    """algorithm and annotation block break class connect connector constant constrainedby der discrete
    each else elseif elsewhen encapsulated end enumeration equation expandable extends external false final
    flow for function if import impure in initial inner input loop model not operator or outer output
    package parameter partial protected public pure record redeclare replaceable return stream then true
    type when while within time Real Integer Boolean String""".split()
)
# names used inside the generated Controller block
RESERVED = {"cycleTime", "fbdProg", "fbdY", "fbdStep", "FbdProgram", "Controller", "Example", "Modelica"}


def package_name(project_name: str) -> str:
    """'03 Tank level' -> 'TankLevel' (a valid Modelica identifier)."""
    words = re.findall(r"[A-Za-z0-9]+", project_name)
    name = "".join(w[:1].upper() + w[1:] for w in words)
    name = re.sub(r"^[0-9]+", "", name)
    if not name or name in MODELICA_KEYWORDS or name in RESERVED:
        name = "FBD" + name if name else "FBDProgram"
    return name[:60]


def platform_dir() -> str:
    bits = "64" if sys.maxsize > 2**32 else "32"
    if sys.platform.startswith("win"):
        return "win" + bits
    if sys.platform == "darwin":
        return "darwin" + bits
    return "linux" + bits


def _q(text: str) -> str:
    """Modelica string literal content."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _mo_name(name: str, used: set[str]) -> str:
    n = name
    while n in MODELICA_KEYWORDS or n in RESERVED or n in used:
        n += "_"
    used.add(n)
    return n


def _literal(type_name: str, value) -> str:
    if type_name == "BOOL":
        return "true" if value else "false"
    if type_name in ("INT", "DINT"):
        return str(int(value))
    r = repr(float(value))
    return r if ("." in r or "e" in r) else r + ".0"


MO_TYPE = {"BOOL": "Boolean", "INT": "Integer", "DINT": "Integer", "REAL": "Real", "TIME": "Real"}


def _to_real(type_name: str, expr: str) -> str:
    return f"(if {expr} then 1.0 else 0.0)" if type_name == "BOOL" else expr


def _from_real(type_name: str, expr: str) -> str:
    if type_name == "BOOL":
        return f"{expr} > 0.5"
    if type_name in ("INT", "DINT"):
        return f"integer({expr} + 0.5)"
    return expr


def package_mo(project: Project, result: CompileResult, pkg: str) -> str:
    """The Modelica source of the package."""
    m = result.model
    assert m is not None
    lib = f"fbd_{pkg}"
    cycle_s = (project.settings.cycle_ms or 1) / 1000.0
    used: set[str] = set()
    ins = [dict(q, mo=_mo_name(q["name"], used)) for q in m["inputs"]]
    outs = [dict(q, mo=_mo_name(q["name"], used)) for q in m["outputs"]]
    pars = [dict(q, mo=_mo_name(q["name"], used)) for q in m["params"]]
    ny = max(1, len(outs))

    # icon: inputs left, outputs right, at least 200 units high
    n = max(len(ins), len(outs), 1)
    pitch = 40 if n > 5 else 200 / (n + 0)
    half = max(100, int(pitch * n / 2 + 0.5))

    def ys(k: int) -> list[float]:
        if k == 0:
            return []
        p = min(pitch, 2 * half / k)
        top = p * (k - 1) / 2
        return [round(top - i * p) for i in range(k)]

    L: list[str] = []
    L.append(f'package {pkg} "FBD Studio controller: {_q(project.name)}"')
    L.append("")
    L.append('  class FbdProgram "State of the PLC program (external object)"')
    L.append("    extends ExternalObject;")
    L.append("    function constructor")
    L.append("      input Real cycleTime;")
    L.append("      input Real p[:];")
    L.append("      output FbdProgram prog;")
    L.append(f'      external "C" prog = {lib}_new(cycleTime, p, size(p, 1)) annotation(Library = "{lib}");')
    L.append("    end constructor;")
    L.append("    function destructor")
    L.append("      input FbdProgram prog;")
    L.append(f'      external "C" {lib}_free(prog) annotation(Library = "{lib}");')
    L.append("    end destructor;")
    L.append("  end FbdProgram;")
    L.append("")
    L.append('  function fbdStep "Run one scan cycle"')
    L.append("    input FbdProgram prog;")
    L.append("    input Real t;")
    L.append("    input Real u[:];")
    L.append(f"    output Real y[{ny}];")
    L.append(f'    external "C" {lib}_step(prog, t, u, size(u, 1), y, size(y, 1)) annotation(Library = "{lib}");')
    L.append("  end fbdStep;")
    L.append("")
    desc = project.description or f"PLC program '{project.name}'"
    L.append(f'  block Controller "{_q(desc)}"')
    L.append(f'    parameter Real cycleTime(unit = "s", min = 1e-6) = {_literal("REAL", cycle_s)} '
             '"Scan cycle time (from the FBD project)" annotation(Dialog(group = "Scan cycle"));')
    for q in pars:
        unit = '(unit = "s")' if q["type"] == "TIME" else ""
        L.append(f'    parameter {MO_TYPE[q["type"]]} {q["mo"]}{unit} = {_literal(q["type"], q["value"])} '
                 f'"{_q(q["description"])}" annotation(Dialog(group = "Constants of the program"));')
    for q, y in zip(ins, ys(len(ins))):
        unit = '(unit = "s")' if q["type"] == "TIME" else ""
        L.append(f'    Modelica.Blocks.Interfaces.{MO_TYPE[q["type"]]}Input {q["mo"]}{unit} "{_q(q["description"])}" '
                 f'annotation(Placement(transformation(extent = {{{{-120, {y - 10}}}, {{-100, {y + 10}}}}})));')
    for q, y in zip(outs, ys(len(outs))):
        unit = '(unit = "s")' if q["type"] == "TIME" else ""
        L.append(f'    Modelica.Blocks.Interfaces.{MO_TYPE[q["type"]]}Output {q["mo"]}{unit} "{_q(q["description"])}" '
                 f'annotation(Placement(transformation(extent = {{{{100, {y - 10}}}, {{120, {y + 10}}}}})));')
    L.append("  protected")
    pvec = "{" + ", ".join(_to_real(q["type"], q["mo"]) for q in pars) + "}" if pars else "fill(0.0, 0)"
    uvec = "{" + ", ".join(_to_real(q["type"], q["mo"]) for q in ins) + "}" if ins else "{0.0}"
    L.append(f"    FbdProgram fbdProg = FbdProgram(cycleTime, {pvec});")
    L.append(f"    discrete Real fbdY[{ny}](each start = 0, each fixed = true);")
    L.append("  algorithm")
    L.append("    when sample(0, cycleTime) then")
    L.append(f"      fbdY := fbdStep(fbdProg, time, {uvec});")
    L.append("    end when;")
    if outs:
        L.append("  equation")
        for k, q in enumerate(outs, 1):
            L.append(f"    {q['mo']} = {_from_real(q['type'], f'fbdY[{k}]')};")
    # icon with connector names
    g = [f"Rectangle(extent = {{{{-100, {half}}}, {{100, {-half}}}}}, lineColor = {{0, 0, 127}}, "
         "fillColor = {255, 255, 255}, fillPattern = FillPattern.Solid)",
         f'Text(extent = {{{{-150, {half + 40}}}, {{150, {half + 5}}}}}, textString = "%name", textColor = {{0, 0, 255}})',
         f'Text(extent = {{{{-40, 20}}, {{40, -20}}}}, textString = "FBD", textColor = {{180, 180, 180}})']
    for q, y in zip(ins, ys(len(ins))):
        g.append(f'Text(extent = {{{{-96, {y + 8}}}, {{-4, {y - 8}}}}}, textString = "{q["mo"]}", '
                 "horizontalAlignment = TextAlignment.Left)")
    for q, y in zip(outs, ys(len(outs))):
        g.append(f'Text(extent = {{{{4, {y + 8}}}, {{96, {y - 8}}}}}, textString = "{q["mo"]}", '
                 "horizontalAlignment = TextAlignment.Right)")
    ext = f"{{{{-100, {-half}}}, {{100, {half}}}}}"
    info = ("<p>Generated by FBD Studio. Each sample period <code>cycleTime</code> the PLC program runs one "
            "scan cycle, with the same C++ code as on the microcontroller; timers use the simulation time.</p>"
            "<p>Inputs: digital inputs are the signal as the program sees it (after 'invert'), analog inputs "
            "are in % of full scale (0..100) and quantised like the real ADC. TIME values are in seconds.</p>"
            "<p>The unconnected inputs of the diagram (setpoints, timer presets, ...) are parameters.</p>")
    L.append(f"    annotation(Icon(coordinateSystem(extent = {ext}), graphics = {{{', '.join(g)}}}),")
    L.append(f'      Documentation(info = "<html>{info}</html>"));')
    L.append("  end Controller;")
    L.append("")
    L.extend(_example(ins, outs))
    L.append("")
    L.append(f'  annotation(uses(Modelica(version = "{MSL_VERSION}")), Documentation(info = "<html>'
             f'<p>Exported from the FBD Studio project <b>{_q(project.name)}</b>. Use <b>Controller</b> in your '
             'model and connect it to a model of the machine or process. <b>Example</b> runs it with constant inputs.</p>'
             '<p>The C++ library is in Resources/Library; to use the package on another operating system, run '
             'the build script in Resources/Source there.</p></html>"));')
    L.append(f"end {pkg};")
    return "\n".join(L) + "\n"


def _example(ins: list[dict], outs: list[dict]) -> list[str]:
    """A model that runs the controller with constant inputs (a starting point and a smoke test)."""
    L = ['  model Example "The controller with constant inputs: replace them by your plant model"',
         "    extends Modelica.Icons.Example;",
         "    Controller controller annotation(Placement(transformation(extent = {{-20, -20}, {20, 20}})));"]
    src = {"BOOL": ("Modelica.Blocks.Sources.BooleanConstant", "false"),
           "INT": ("Modelica.Blocks.Sources.IntegerConstant", "0"),
           "DINT": ("Modelica.Blocks.Sources.IntegerConstant", "0"),
           "REAL": ("Modelica.Blocks.Sources.Constant", "0"),
           "TIME": ("Modelica.Blocks.Sources.Constant", "0")}
    eqs = []
    for i, q in enumerate(ins):
        cls, k = src[q["type"]]
        y = 60 - 30 * i
        L.append(f"    {cls} in_{q['mo']}(k = {k}) annotation(Placement(transformation(extent = {{{{-80, {y - 10}}}, {{-60, {y + 10}}}}})));")
        eqs.append(f"    connect(in_{q['mo']}.y, controller.{q['mo']});")
    L.append("  equation")
    L.extend(eqs)
    L.append("    annotation(experiment(StopTime = 10, Interval = 0.01));")
    L.append("  end Example;")
    return L


def library_code(result: CompileResult, pkg: str) -> str:
    assert result.code is not None
    return result.code.replace("@@ID@@", f"fbd_{pkg}")


def build_script(pkg: str, windows: bool) -> str:
    lib = f"fbd_{pkg}"
    flags = "-std=c++11 -O2 -fno-exceptions -fno-rtti -fno-threadsafe-statics -fvisibility=hidden -w -I."
    if windows:
        return ("@echo off\r\nrem Rebuilds the controller library for Windows (needs g++ and ar, e.g. from MSYS2).\r\n"
                "cd /d \"%~dp0\"\r\nif not exist ..\\Library\\win64 mkdir ..\\Library\\win64\r\n"
                f"g++ {flags} -c {lib}.cpp -o {lib}.o || exit /b 1\r\n"
                f"ar rcs ..\\Library\\win64\\lib{lib}.a {lib}.o || exit /b 1\r\n"
                f"del {lib}.o\r\necho Built Resources\\Library\\win64\\lib{lib}.a\r\n")
    return ("#!/bin/sh\n# Rebuilds the controller library for this system (needs g++ or clang++ and ar).\nset -e\n"
            'cd "$(dirname "$0")"\n'
            'case "$(uname -s)" in Darwin) P=darwin64;; *) P=linux64;; esac\n'
            'mkdir -p "../Library/$P"\n'
            f"${{CXX:-g++}} {flags} -fPIC -c {lib}.cpp -o {lib}.o\n"
            f'ar rcs "../Library/$P/lib{lib}.a" {lib}.o\nrm -f {lib}.o\n'
            f'echo "Built Resources/Library/$P/lib{lib}.a"\n')


def build_library(result: CompileResult, pkg: str, out_dir: Path) -> Path:
    """Compile the program into Resources/Library/<platform>/libfbd_<pkg>.a below out_dir."""
    cxx = find_cxx()
    ar = shutil.which("ar") or shutil.which("gcc-ar") or shutil.which("llvm-ar")
    if cxx is None or ar is None:
        raise RuntimeError("The Modelica export needs a C++ compiler (g++ or clang++) and 'ar'. Please install them.")
    lib = f"fbd_{pkg}"
    dest = out_dir / "Resources" / "Library" / platform_dir() / f"lib{lib}.a"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"{lib}.cpp"
        src.write_text(library_code(result, pkg), encoding="utf-8")
        obj = Path(tmp) / f"{lib}.o"
        cmd = [cxx, "-std=c++11", "-O2", "-fno-exceptions", "-fno-rtti", "-fno-threadsafe-statics",
               "-fvisibility=hidden", "-w", f"-I{MODEL_DIR}", f"-I{RUNTIME_DIR}", "-c", str(src), "-o", str(obj)]
        if os.name != "nt":
            cmd.insert(2, "-fPIC")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError("Compiling the Modelica library failed:\n" + (proc.stderr or proc.stdout)[-4000:])
        if dest.exists():
            dest.unlink()
        proc = subprocess.run([ar, "rcs", str(dest), str(obj)], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError("Creating the Modelica library failed:\n" + (proc.stderr or proc.stdout)[-2000:])
    return dest


def write_package(project: Project, result: CompileResult, out_root: Path) -> Path:
    """Write the complete package directory <out_root>/<Pkg>; returns its path."""
    pkg = package_name(project.name)
    d = out_root / pkg
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    (d / "package.mo").write_text(package_mo(project, result, pkg), encoding="utf-8")
    src = d / "Resources" / "Source"
    src.mkdir(parents=True)
    (src / f"fbd_{pkg}.cpp").write_text(library_code(result, pkg), encoding="utf-8")
    shutil.copy(MODEL_DIR / "Arduino.h", src / "Arduino.h")
    for h in sorted(set(re.findall(r'^#include "([^"]+)"', result.code or "", re.M)) - {"Arduino.h"}):
        if (RUNTIME_DIR / h).is_file():  # plcrt.h and the helpers of user blocks
            shutil.copy(RUNTIME_DIR / h, src / h)
    (src / "build.sh").write_text(build_script(pkg, windows=False), encoding="utf-8", newline="\n")
    (src / "build.sh").chmod(0o755)
    (src / "build.bat").write_text(build_script(pkg, windows=True), encoding="utf-8", newline="")
    build_library(result, pkg, d)
    return d


def zip_package(project: Project, result: CompileResult) -> tuple[str, bytes]:
    """The package as a zip file: (package name, bytes)."""
    with tempfile.TemporaryDirectory() as tmp:
        d = write_package(project, result, Path(tmp))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(d.rglob("*")):
                if f.is_file():
                    info = zipfile.ZipInfo(str(f.relative_to(tmp)).replace(os.sep, "/"), time.localtime()[:6])
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (0o755 if f.name == "build.sh" else 0o644) << 16
                    z.writestr(info, f.read_bytes())
        return d.name, buf.getvalue()
