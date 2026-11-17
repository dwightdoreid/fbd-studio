"""Diagram validation, execution ordering and C++ code generation.

Execution model (like a PLC scan cycle):
  * every block instance becomes a C++ struct with its inputs, outputs and
    internal variables as members, plus init() and cycle() methods;
  * each scan, blocks run in data-flow order: a block's wired inputs are copied
    from the source outputs, then its cycle() runs;
  * feedback loops are allowed: the wire that closes a loop simply delivers the
    value from the previous scan (it is marked as a feedback wire);
  * unconnected inputs hold constants that are assigned once at start-up, so
    they can be changed online through the monitor.
"""

from __future__ import annotations

import hashlib
import heapq
import textwrap
from dataclasses import dataclass, field
from typing import Any, Optional

from . import types as T
from .blocks import BlockDef, Library
from .boards import BoardDef
from .project import BlockInstance, Project

CPP_RESERVED = set(
    """alignas alignof and and_eq asm auto bitand bitor bool break case catch char char16_t char32_t
    class compl const constexpr const_cast continue decltype default delete do double dynamic_cast else
    enum explicit export extern false float for friend goto if inline int long mutable namespace new
    noexcept not not_eq nullptr operator or or_eq private protected public register reinterpret_cast
    return short signed sizeof static static_assert static_cast struct switch template this thread_local
    throw true try typedef typeid typename union unsigned using virtual void volatile wchar_t while xor
    xor_eq setup loop main plc Serial HIGH LOW INPUT OUTPUT INPUT_PULLUP INPUT_PULLDOWN LED_BUILTIN
    PI HALF_PI TWO_PI DEG_TO_RAD RAD_TO_DEG min max abs round constrain map millis micros delay yield
    SYMS SYM_COUNT PROGRAM_HASH""".split()
)


@dataclass
class Diagnostic:
    level: str  # "error" | "warning" | "info"
    message: str
    block: Optional[str] = None
    wire: Optional[str] = None
    port: Optional[str] = None

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Port:
    name: str  # name as used in the diagram, e.g. "IN3"
    base: str  # struct member name, e.g. "IN"
    index: Optional[int]  # array index for variadic inputs
    type: str  # concrete IEC type
    default: Any = None

    def member(self) -> str:
        return self.base if self.index is None else f"{self.base}[{self.index}]"


@dataclass
class Resolved:
    inst: BlockInstance
    bdef: BlockDef
    type_args: dict[str, str]
    counts: dict[str, int]
    inputs: dict[str, Port]
    outputs: dict[str, Port]


@dataclass
class CompileResult:
    diagnostics: list[Diagnostic] = field(default_factory=list)
    order: list[str] = field(default_factory=list)  # block ids in execution order
    feedback: list[str] = field(default_factory=list)  # wire ids that close a loop
    ports: dict[str, dict] = field(default_factory=dict)  # block id -> resolved port types
    code: Optional[str] = None
    runtime_hash: Optional[str] = None
    symbols: list[dict] = field(default_factory=list)
    lib_deps: list[str] = field(default_factory=list)
    platformio: dict[str, str] = field(default_factory=dict)  # extra platformio.ini options from blocks
    model: Optional[dict] = None  # target "model": Modelica connectors and parameters (see modelica.py)

    @property
    def ok(self) -> bool:
        return not any(d.level == "error" for d in self.diagnostics)

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "diagnostics": [d.to_json() for d in self.diagnostics],
            "order": self.order,
            "feedback": self.feedback,
            "ports": self.ports,
            "code": self.code,
            "hash": self.runtime_hash,
            "symbols": self.symbols,
            "lib_deps": self.lib_deps,
            "platformio": self.platformio,
        }


def c_type(type_name: str, type_params: dict) -> str:
    return type_name if type_name in type_params else T.C_TYPES[type_name]


# ====================================================================== resolve

def _resolve_instance(inst: BlockInstance, bdef: BlockDef, diags: list[Diagnostic]) -> Resolved:
    type_args: dict[str, str] = {}
    for tp_name, tp in bdef.type_params.items():
        allowed = T.expand_allowed(tp.allowed)
        val = inst.type_args.get(tp_name, tp.default)
        if val not in allowed:
            diags.append(Diagnostic("error", f"{inst.name}: type {tp_name}={val} not allowed (use {', '.join(allowed)})", inst.id))
            val = tp.default
        type_args[tp_name] = val

    def concrete(t: str) -> str:
        return type_args.get(t, t)

    counts: dict[str, int] = {}
    inputs: dict[str, Port] = {}
    for p in bdef.inputs:
        if p.variadic:
            v = p.variadic
            n = inst.counts.get(p.name, v.default)
            if not isinstance(n, int) or not v.min <= n <= v.max:
                diags.append(Diagnostic("error", f"{inst.name}: {p.name} needs {v.min}..{v.max} inputs", inst.id))
                n = max(v.min, min(v.max, n if isinstance(n, int) else v.default))
            counts[p.name] = n
            for i in range(n):
                name = f"{p.name}{i + v.start}"
                inputs[name] = Port(name, p.name, i, concrete(p.type), p.default)
        else:
            inputs[p.name] = Port(p.name, p.name, None, concrete(p.type), p.default)
    outputs = {p.name: Port(p.name, p.name, None, concrete(p.type), None) for p in bdef.outputs}
    return Resolved(inst, bdef, type_args, counts, inputs, outputs)


def _param_c_value(r: Resolved, pdef, board: Optional[BoardDef], target: str, diags, pins_used) -> Optional[str]:
    inst = r.inst
    raw = inst.params.get(pdef.name, pdef.default)
    where = f"{inst.name}.{pdef.name}"
    if pdef.kind == "pin":
        if isinstance(raw, str) and raw.startswith("@"):
            raw = board.pin_defaults.get(raw[1:]) if board else None
        if raw in (None, ""):
            diags.append(Diagnostic("error", f"{where}: select a pin", inst.id, port=pdef.name))
            return None
        if board is None:
            return None
        pin = board.pin(str(raw))
        if pin is None:
            diags.append(Diagnostic("error", f"{where}: pin '{raw}' does not exist on {board.title}", inst.id, port=pdef.name))
            return None
        if pdef.capability not in pin.caps:
            diags.append(Diagnostic("error", f"{where}: pin {pin.id} cannot be used as {pdef.capability} on {board.title}", inst.id, port=pdef.name))
        # no warning when this is the board's intended pin for the parameter (e.g. the built-in display's CS)
        intended = isinstance(pdef.default, str) and pdef.default.startswith("@") and board.pin_defaults.get(pdef.default[1:]) == pin.id
        if pin.warn and pin.note and not intended:
            diags.append(Diagnostic("warning", f"{where}: {pin.id}: {pin.note}", inst.id, port=pdef.name))
        pins_used.setdefault(pin.id, []).append((inst, pdef.capability, pdef.shared))
        return str(board.pin_index(pin.id)) if target in ("sim", "model") else pin.c
    if pdef.kind == "enum":
        if raw not in pdef.options:
            diags.append(Diagnostic("error", f"{where}: '{raw}' is not one of {', '.join(pdef.options)}", inst.id, port=pdef.name))
            return None
        return pdef.options[raw]
    if pdef.kind == "string":
        text = "" if raw is None else str(raw)
        if pdef.required and not text.strip():
            diags.append(Diagnostic("error", f"{where}: enter a value", inst.id, port=pdef.name))
        if len(text) > 200:
            diags.append(Diagnostic("error", f"{where}: text is too long (max. 200 characters)", inst.id, port=pdef.name))
        return T.c_string(text)
    if pdef.kind == "bool":
        try:
            return T.c_literal("BOOL", False if raw is None else raw)
        except ValueError as e:
            diags.append(Diagnostic("error", f"{where}: {e}", inst.id, port=pdef.name))
            return None
    # int / real
    try:
        if isinstance(raw, str) and pdef.kind == "int":
            raw = int(raw.strip(), 0)  # also accepts 0x3C, 0b101
        v = float(0 if raw is None else raw)
    except (TypeError, ValueError):
        diags.append(Diagnostic("error", f"{where}: '{raw}' is not a number", inst.id, port=pdef.name))
        return None
    if (pdef.min is not None and v < pdef.min) or (pdef.max is not None and v > pdef.max):
        diags.append(Diagnostic("error", f"{where}: {raw} outside {pdef.min}..{pdef.max}", inst.id, port=pdef.name))
    if pdef.kind == "int":
        if v != int(v):
            diags.append(Diagnostic("error", f"{where}: must be a whole number", inst.id, port=pdef.name))
        return str(int(v))
    return T.c_literal("REAL", v)


def _check_features(resolved: dict, board: BoardDef, pins_used: dict, diags: list) -> None:
    by_type: dict[str, list] = {}
    for r in resolved.values():
        by_type.setdefault(r.bdef.type, []).append(r)
    used_features: set[str] = set()
    for type_name, rs in by_type.items():
        bdef = rs[0].bdef
        missing = [f.replace("|", " or ") for f in bdef.requires if not any(a in board.features for a in f.split("|"))]
        for r in rs:
            if missing:
                diags.append(Diagnostic("error", f"{r.inst.name}: {type_name} needs {', '.join(missing)}, which {board.title} does not have", r.inst.id))
        for f in bdef.requires:
            used_features.update(a for a in f.split("|") if a in board.features)
        if bdef.singleton and len(rs) > 1:
            for r in rs:
                diags.append(Diagnostic("error", f"{r.inst.name}: only one {type_name} block is allowed per program", r.inst.id))
        for need in bdef.requires_blocks:
            if not any(alt in by_type for alt in need.split("|")):
                for r in rs:
                    diags.append(Diagnostic("error", f"{r.inst.name}: {type_name} needs a {need.replace('|', ' or ')} block in the diagram", r.inst.id))
        for other in bdef.excludes_blocks:
            if other in by_type:
                for r in rs:
                    diags.append(Diagnostic("error", f"{r.inst.name}: {type_name} and {other} cannot be used in the same program", r.inst.id))
    for rule in board.restrictions:
        if rule.feature not in used_features:
            continue
        for pin_id in rule.pins:
            for inst, cap, _ in pins_used.get(pin_id, []):
                if cap == rule.capability:
                    diags.append(Diagnostic(rule.level, f"{inst.name}: {pin_id} {rule.message}", inst.id))


# ====================================================================== ordering

def _execution_order(blocks: list[BlockInstance], edges: list[tuple[str, str, str]]):
    """Kahn's topological sort, ties broken by position (left-to-right, top-to-bottom).

    edges: (src block id, dst block id, wire id). Returns (order, feedback wire ids).
    When the remaining graph only contains cycles, the block that comes first on
    the canvas inside a "source" strongly-connected component is started, and the
    wires into it from unprocessed blocks become feedback wires.
    """
    pos = {b.id: (round(b.x), round(b.y), b.name) for b in blocks}
    incoming: dict[str, list[tuple[str, str]]] = {b.id: [] for b in blocks}
    outgoing: dict[str, list[tuple[str, str]]] = {b.id: [] for b in blocks}
    for s, d, w in edges:
        incoming[d].append((s, w))
        outgoing[s].append((d, w))
    scc = _scc_ids([b.id for b in blocks], outgoing)

    done: set[str] = set()
    feedback: set[str] = set()
    order: list[str] = []
    indeg = {b: len(incoming[b]) for b in incoming}
    heap = [(pos[b], b) for b, n in indeg.items() if n == 0]
    heapq.heapify(heap)
    while len(order) < len(blocks):
        if not heap:
            remaining = [b for b in indeg if b not in done]
            # blocks whose pending inputs all come from inside their own loop
            candidates = [
                b for b in remaining
                if all(s in done or w in feedback or scc[s] == scc[b] for s, w in incoming[b])
            ] or remaining
            start = min(candidates, key=lambda b: pos[b])
            for s, w in incoming[start]:
                if s not in done and w not in feedback:
                    feedback.add(w)
                    indeg[start] -= 1
            heapq.heappush(heap, (pos[start], start))
        _, b = heapq.heappop(heap)
        if b in done:
            continue
        done.add(b)
        order.append(b)
        for d, w in outgoing[b]:
            if w in feedback:
                continue
            indeg[d] -= 1
            if indeg[d] == 0 and d not in done:
                heapq.heappush(heap, (pos[d], d))
    return order, sorted(feedback)


def _scc_ids(nodes: list[str], outgoing: dict[str, list[tuple[str, str]]]) -> dict[str, int]:
    """Iterative Tarjan: map node -> strongly connected component id."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    comp: dict[str, int] = {}
    counter = 0
    n_comps = 0
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(outgoing[root]))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt, _ in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(outgoing[nxt])))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                cid = n_comps
                n_comps += 1
                while True:
                    m = stack.pop()
                    on_stack.discard(m)
                    comp[m] = cid
                    if m == node:
                        break
    return comp


# ====================================================================== compile

def compile_project(project: Project, library: Library, board: Optional[BoardDef], target: str = "device") -> CompileResult:
    res = CompileResult()
    diags = res.diagnostics
    if board is None:
        diags.append(Diagnostic("error", f"Unknown board '{project.board}'"))
    if not project.blocks:
        diags.append(Diagnostic("info", "The diagram is empty. Drag blocks from the library onto the canvas."))

    # ---- instances
    resolved: dict[str, Resolved] = {}
    names: dict[str, str] = {}
    for inst in project.blocks:
        bdef = library.get(inst.type)
        if bdef is None:
            diags.append(Diagnostic("error", f"{inst.name}: unknown block type '{inst.type}' (missing user block?)", inst.id))
            continue
        if not T.IDENT_RE.match(inst.name) or inst.name in CPP_RESERVED or inst.name.startswith("FB_"):
            diags.append(Diagnostic("error", f"'{inst.name}' is not a valid instance name (letters, digits, _; must not start with a digit or be a reserved word)", inst.id))
        elif inst.name in names:
            diags.append(Diagnostic("error", f"Duplicate instance name '{inst.name}'", inst.id))
        names[inst.name] = inst.id
        resolved[inst.id] = _resolve_instance(inst, bdef, diags)
    for b in resolved.values():
        res.ports[b.inst.id] = {
            "inputs": {n: p.type for n, p in b.inputs.items()},
            "outputs": {n: p.type for n, p in b.outputs.items()},
        }

    # ---- wires
    wired: dict[tuple[str, str], tuple[Resolved, Port, str]] = {}  # (dst block, dst port) -> source
    edges: list[tuple[str, str, str]] = []
    for w in project.wires:
        s = resolved.get(w.src.block)
        d = resolved.get(w.dst.block)
        if s is None or d is None:
            if w.src.block in {b.id for b in project.blocks} and w.dst.block in {b.id for b in project.blocks}:
                continue  # endpoint block already reported as unknown type
            diags.append(Diagnostic("error", "Wire connects to a missing block", wire=w.id))
            continue
        sp = s.outputs.get(w.src.port)
        dp = d.inputs.get(w.dst.port)
        if sp is None:
            diags.append(Diagnostic("error", f"{s.inst.name} has no output '{w.src.port}'", s.inst.id, w.id))
            continue
        if dp is None:
            diags.append(Diagnostic("error", f"{d.inst.name} has no input '{w.dst.port}'", d.inst.id, w.id))
            continue
        key = (d.inst.id, dp.name)
        if key in wired:
            diags.append(Diagnostic("error", f"{d.inst.name}.{dp.name} has more than one wire", d.inst.id, w.id, dp.name))
            continue
        if not T.compatible(sp.type, dp.type):
            hint = ""
            if sp.type != "BOOL" and dp.type != "BOOL":
                hint = f" (insert a TO_{dp.type} conversion block)"
            diags.append(Diagnostic(
                "error",
                f"Type mismatch: {s.inst.name}.{sp.name} is {sp.type} but {d.inst.name}.{dp.name} expects {dp.type}{hint}",
                d.inst.id, w.id, dp.name,
            ))
            continue
        wired[key] = (s, sp, w.id)
        edges.append((s.inst.id, d.inst.id, w.id))

    # ---- parameters, constants
    pins_used: dict[str, list] = {}
    param_values: dict[str, dict[str, str]] = {}
    const_values: dict[str, dict[str, str]] = {}
    for r in resolved.values():
        pv = {}
        for pdef in r.bdef.params:
            val = _param_c_value(r, pdef, board, target, diags, pins_used)
            if val is not None:
                pv[pdef.name] = val
        param_values[r.inst.id] = pv
        cv = {}
        for pname, port in r.inputs.items():
            if (r.inst.id, pname) in wired:
                continue
            raw = r.inst.values.get(pname)
            if raw is None or raw == "":
                raw = port.default if port.default is not None else T.zero_value(port.type)
            try:
                cv[pname] = T.c_literal(port.type, raw)
            except ValueError as e:
                diags.append(Diagnostic("error", f"{r.inst.name}.{pname}: {e}", r.inst.id, port=pname))
        const_values[r.inst.id] = cv
        for pname in r.inst.values:
            if pname not in r.inputs:
                pass  # stale value for a removed variadic input: ignore silently

    # ---- pin conflicts
    for pin_id, users in pins_used.items():
        outputs = [u for u in users if u[1] not in ("DI", "AI")]
        all_shared = all(u[2] for u in users) and len({u[1] for u in users}) == 1
        if len(users) > 1 and outputs and not all_shared:
            names_ = ", ".join(f"{i.name} ({cap})" for i, cap, _ in users)
            for inst, _, _ in users:
                diags.append(Diagnostic("error", f"Pin {pin_id} is used by several blocks, including an output: {names_}", inst.id))

    # ---- board features, singletons, required blocks
    if board is not None:
        _check_features(resolved, board, pins_used, diags)

    # ---- order
    live_blocks = [r.inst for r in resolved.values()]
    order, feedback = _execution_order(live_blocks, edges)
    res.order, res.feedback = order, feedback
    wire_by_id = {w.id: w for w in project.wires}
    for wid in feedback:
        w = wire_by_id[wid]
        diags.append(Diagnostic(
            "info",
            f"Feedback loop: {resolved[w.dst.block].inst.name}.{w.dst.port} uses the value of "
            f"{resolved[w.src.block].inst.name}.{w.src.port} from the previous scan cycle",
            w.dst.block, wid,
        ))

    if target == "model":
        for r in resolved.values():
            if r.bdef.model is None and "sim" in r.bdef.targets:
                diags.append(Diagnostic(
                    "error", f"{r.inst.name}: {r.bdef.type} cannot be used in a Modelica export "
                    "(the block has no 'model:' section)", r.inst.id))

    if not res.ok or board is None:
        return res

    if target == "model":
        _generate_model(res, project, board, resolved, order, wired, set(feedback), param_values, const_values)
        return res
    _generate(res, project, board, target, resolved, order, wired, set(feedback), param_values, const_values)
    return res


# ====================================================================== codegen

def _indent(code: str, n: int) -> str:
    code = textwrap.dedent(code).strip("\n")
    return textwrap.indent(code, " " * n) if code else ""


def _struct(bdef: BlockDef, target: str) -> str:
    code = bdef.code_for(target)
    tps = bdef.type_params
    lines: list[str] = []
    title = f"{bdef.type}: {bdef.title}" if bdef.title else bdef.type
    lines.append(f"// ---- {title}")
    if tps:
        lines.append("template <" + ", ".join(f"typename {t}" for t in tps) + ">")
    lines.append(f"struct FB_{bdef.type} {{")
    if bdef.inputs:
        lines.append("  // inputs")
        for p in bdef.inputs:
            ct = c_type(p.type, tps)
            if p.variadic:
                lines.append(f"  {ct} {p.name}[{p.variadic.max}] = {{}};")
                lines.append(f"  uint8_t {p.name}_n = {p.variadic.default};")
            else:
                lines.append(f"  {ct} {p.name}{{}};")
    if bdef.outputs:
        lines.append("  // outputs")
        for p in bdef.outputs:
            lines.append(f"  {c_type(p.type, tps)} {p.name}{{}};")
    vars_ = list(bdef.vars)
    if target == "model" and bdef.model is not None:
        vars_ += bdef.model.vars
    if vars_:
        lines.append("  // internal state")
        for v in vars_:
            ct = c_type(v.type, tps)
            if v.init is None:
                lines.append(f"  {ct} {v.name}{{}};")
            elif v.type in tps:
                lines.append(f"  {ct} {v.name} = ({ct})({v.init});")
            else:
                lines.append(f"  {ct} {v.name} = {T.c_literal(v.type, v.init)};")
    if bdef.params:
        lines.append("  // parameters")
        ptypes = {"pin": "int", "enum": "int", "int": "int32_t", "real": "float", "bool": "bool", "string": "const char*"}
        for p in bdef.params:
            lines.append(f"  {ptypes[p.kind]} {p.name}{{}};")
    if code["members"].strip():
        lines.append("  // driver state")
        lines.append(_indent(code["members"], 2))
    lines.append("")
    lines.append("  void init() {")
    if code["init"].strip():
        lines.append(_indent(code["init"], 4))
    lines.append("  }")
    lines.append("  void cycle() {")
    if code["cycle"].strip():
        lines.append(_indent(code["cycle"], 4))
    lines.append("  }")
    lines.append("};")
    return "\n".join(lines)


def _generate(res, project, board, target, resolved, order, wired, feedback, param_values, const_values):
    used_types: list[BlockDef] = []
    for bid in order:
        bdef = resolved[bid].bdef
        if bdef not in used_types:
            used_types.append(bdef)

    includes: list[str] = []
    globals_: list[str] = []
    lib_deps: list[str] = []
    for bdef in used_types:
        code = bdef.code_for(target)
        for inc in code["includes"]:
            inc = inc if inc.startswith(("<", '"')) else f"<{inc}>"
            if inc not in includes:
                includes.append(inc)
        if code["globals"].strip():
            globals_.append(f"// globals of {bdef.type}\n{textwrap.dedent(code['globals']).strip()}")
        for dep in bdef.lib_deps:
            if dep not in lib_deps:
                lib_deps.append(dep)
        for key, val in bdef.platformio.items():
            if key in res.platformio and res.platformio[key] != val:
                res.diagnostics.append(Diagnostic("warning", f"{bdef.type}: platformio option {key} conflicts with another block ({res.platformio[key]} vs {val})"))
            res.platformio.setdefault(key, val)
    res.lib_deps = lib_deps

    out: list[str] = []
    out.append(f"// Generated by FBD Studio from project \"{project.name}\" for {board.title} ({target}).")
    out.append("// This file is readable on purpose: you can take it and continue by hand.")
    out.append("#include <Arduino.h>")
    for inc in includes:
        out.append(f"#include {inc}")
    out.append('#include "plcrt.h"')
    out.append("")
    out.append(f"#ifndef PLC_ADC_MAX\n#define PLC_ADC_MAX {board.adc_max}\n#endif")
    out.append("")
    if globals_:
        out.extend(globals_)
        out.append("")
    out.append("// ================================================================ block types")
    for bdef in used_types:
        out.append(_struct(bdef, target))
        out.append("")

    out.append("// ================================================================ instances")
    for bid in order:
        r = resolved[bid]
        tmpl = ""
        if r.bdef.type_params:
            tmpl = "<" + ", ".join(T.C_TYPES[r.type_args[t]] for t in r.bdef.type_params) + ">"
        comment = f"  // {r.inst.comment}" if r.inst.comment else ""
        out.append(f"FB_{r.bdef.type}{tmpl} {r.inst.name};{comment}")
    out.append("")

    # ---- symbols for online monitoring
    symbols: list[dict] = []
    keys: set[int] = set()
    for bid in order:
        r = resolved[bid]
        for p in r.outputs.values():
            symbols.append({"block": bid, "port": p.name, "type": p.type, "dir": "out", "writable": False,
                            "c": f"{r.inst.name}.{p.member()}"})
        for p in r.inputs.values():
            if (bid, p.name) not in wired:
                key = _saved_value_key(project.id, bid, r.bdef.type, p.name, p.type, const_values[bid].get(p.name, ""))
                while key in keys:  # practically never; keep keys unique within a program
                    key = (key * 2654435761 + 1) & 0xFFFFFFFF or 1
                keys.add(key)
                raw = r.inst.values.get(p.name)
                if raw is None or raw == "":
                    raw = p.default if p.default is not None else T.zero_value(p.type)
                try:
                    init = T.parse_value(p.type, raw)  # the project's value (TIME in ms)
                except ValueError:
                    init = None
                symbols.append({"block": bid, "port": p.name, "type": p.type, "dir": "in", "writable": True,
                                "c": f"{r.inst.name}.{p.member()}", "key": key, "init": init})
    for i, s in enumerate(symbols):
        s["idx"] = i
    out.append("// ================================================================ monitoring symbols")
    if symbols:
        out.append("static const plc::Sym SYMS[] = {")
        for s in symbols:
            out.append(f"  {{&{s['c']}, plc::T_{s['type']}, {1 if s['writable'] else 0}, 0x{s.get('key', 0):08x}u}},  // {s['idx']}")
        out.append("};")
        out.append("static const uint16_t SYM_COUNT = sizeof(SYMS) / sizeof(SYMS[0]);")
    else:
        out.append("static const plc::Sym* const SYMS = nullptr;")
        out.append("static const uint16_t SYM_COUNT = 0;")
    out.append('static const char PROGRAM_HASH[] = "@@HASH@@";')
    out.append("")

    # ---- setup
    s = project.settings
    out.append("void setup() {")
    out.append(f"  plc::begin(SYMS, SYM_COUNT, PROGRAM_HASH, {s.baud}, {s.cycle_ms}, {s.monitor_ms});")
    for bid in order:
        r = resolved[bid]
        lines = []
        for name, val in param_values[bid].items():
            lines.append(f"  {r.inst.name}.{name} = {val};")
        for base, n in r.counts.items():
            lines.append(f"  {r.inst.name}.{base}_n = {n};")
        for pname, lit in const_values[bid].items():
            lines.append(f"  {r.inst.name}.{r.inputs[pname].member()} = {lit};")
        if lines:
            out.append(f"  // {r.inst.name} ({r.bdef.type})")
            out.extend(lines)
    out.append("  // values saved on the controller (\"Save values\" while online) replace the ones above")
    out.append("  plc::restore();")
    for bid in order:
        out.append(f"  {resolved[bid].inst.name}.init();")
    out.append("}")
    out.append("")

    # ---- loop
    out.append("void loop() {")
    out.append("  plc::cycleBegin();")
    for n, bid in enumerate(order, 1):
        r = resolved[bid]
        out.append(f"  // [{n}] {r.inst.name} ({r.bdef.type})")
        for pname, port in r.inputs.items():
            src = wired.get((bid, pname))
            if not src:
                continue
            sres, sport, wid = src
            expr = f"{sres.inst.name}.{sport.member()}"
            if sport.type != port.type:
                expr = f"({T.C_TYPES[port.type]}){expr}"
            fb = "  // feedback: value from previous scan" if wid in feedback else ""
            out.append(f"  {r.inst.name}.{port.member()} = {expr};{fb}")
        out.append(f"  {r.inst.name}.cycle();")
    out.append("  plc::cycleEnd();")
    out.append("}")
    out.append("")

    code = "\n".join(out)
    h = hashlib.sha1(code.encode()).hexdigest()[:8]
    res.code = code.replace("@@HASH@@", h)
    res.runtime_hash = h
    for sym in symbols:
        sym.pop("c")
        sym.pop("key", None)
    res.symbols = symbols


def _saved_value_key(project_id: str, block_id: str, block_type: str, port: str, type_name: str, literal: str) -> int:
    """Key of a saved value (see "saved values" in runtime/plcrt.h).

    The project id keeps values saved by one program from applying to another one;
    the block type and id, the input and its type tie the value to its input; and the
    input's value in the project makes a saved value stop applying once that value is
    changed in the diagram and uploaded again.
    """
    h = hashlib.sha1(f"{project_id}/{block_id}/{block_type}.{port}:{type_name}={literal}".encode()).digest()
    return int.from_bytes(h[:4], "little") or 1


# ====================================================================== Modelica export

MODEL_SYSTEM_INCLUDES = ["<stdint.h>", "<stddef.h>", "<stdio.h>", "<stdlib.h>", "<string.h>", "<math.h>",
                         "<limits>", "<type_traits>", "<new>"]


def _model_from_double(type_name: str, expr: str) -> str:
    """C++ expression converting a Modelica Real to an IEC type."""
    if type_name == "BOOL":
        return f"({expr} > 0.5)"
    if type_name == "REAL":
        return f"(float)({expr})"
    if type_name == "TIME":  # Modelica side: seconds
        return f"plc::conv<int32_t>(({expr}) * 1000.0)"
    return f"plc::conv<{T.C_TYPES[type_name]}>({expr})"


def _model_to_double(type_name: str, expr: str) -> str:
    if type_name == "BOOL":
        return f"({expr} ? 1.0 : 0.0)"
    if type_name == "TIME":
        return f"(double)({expr}) / 1000.0"
    return f"(double)({expr})"


def _model_value(type_name: str, raw) -> Any:
    """Python value of a constant as Modelica sees it (TIME in seconds)."""
    v = T.parse_value(type_name, raw)
    return v / 1000.0 if type_name == "TIME" else v


def _generate_model(res, project, board, resolved, order, wired, feedback, param_values, const_values):
    """C++ for the Modelica export: the program as a struct plus a C API.

    extern "C" fbd_<id>_new(cycle_s, params, n) / _step(h, time_s, u, nu, y, ny) / _free(h).
    u, y and params are Real vectors; the order is recorded in res.model.
    """
    used_types: list[BlockDef] = []
    for bid in order:
        if resolved[bid].bdef not in used_types:
            used_types.append(resolved[bid].bdef)
    sys_includes = list(MODEL_SYSTEM_INCLUDES)
    local_includes: list[str] = []
    globals_: list[str] = []
    for bdef in used_types:
        code = bdef.code_for("model")
        for inc in code["includes"]:
            inc = inc if inc.startswith(("<", '"')) else f"<{inc}>"
            target_list = sys_includes if inc.startswith("<") else local_includes
            if inc not in target_list:
                target_list.append(inc)
        if code["globals"].strip():
            globals_.append(f"// globals of {bdef.type}\n{textwrap.dedent(code['globals']).strip()}")

    inputs: list[dict] = []  # Modelica inputs, in u[] order
    outputs: list[dict] = []  # Modelica outputs, in y[] order
    params: list[dict] = []  # Modelica parameters (unwired inputs of logic blocks), in p[] order

    def describe(r: Resolved) -> str:
        pins = [f"{k} {v}" for k, v in r.inst.params.items()
                if any(p.name == k and p.kind == "pin" for p in r.bdef.params) and v]
        text = r.bdef.type + (f" ({', '.join(pins)})" if pins else "")
        return text + (f": {r.inst.comment}" if r.inst.comment else "")

    by_position = sorted(order, key=lambda b: (resolved[b].inst.y, resolved[b].inst.x))  # icon follows the diagram
    for bid in by_position:
        r = resolved[bid]
        m = r.bdef.model
        if m is not None:
            n_io = len(m.from_model) + len(m.to_model)
            for port in m.from_model:
                p = r.outputs[port]
                inputs.append({"name": r.inst.name if n_io == 1 else f"{r.inst.name}_{port}", "type": p.type,
                               "block": bid, "port": port, "c": f"{r.inst.name}.{p.member()}", "description": describe(r)})
            for port in m.to_model:
                p = r.inputs[port]
                outputs.append({"name": r.inst.name if n_io == 1 else f"{r.inst.name}_{port}", "type": p.type,
                                "block": bid, "port": port, "c": f"{r.inst.name}.{p.member()}", "description": describe(r)})
        else:
            for pname, port in r.inputs.items():
                if (bid, pname) in wired:
                    continue
                raw = r.inst.values.get(pname)
                if raw is None or raw == "":
                    raw = port.default if port.default is not None else T.zero_value(port.type)
                params.append({"name": f"{r.inst.name}_{pname}", "type": port.type, "value": _model_value(port.type, raw),
                               "block": bid, "port": pname, "c": f"{r.inst.name}.{port.member()}",
                               "description": f"{r.inst.name} ({r.bdef.type}) input {pname}"})

    out: list[str] = []
    out.append(f"// Generated by FBD Studio from project \"{project.name}\" for a Modelica export.")
    out.append("// The PLC program runs once per scan cycle, called from Modelica (see package.mo).")
    for inc in sys_includes:
        out.append(f"#include {inc}")
    out.append("")
    out.append("namespace {  // internal linkage: several exported programs can live in one model")
    out.append('#include "Arduino.h"')
    out.append(f"#ifndef PLC_ADC_MAX\n#define PLC_ADC_MAX {board.adc_max}\n#endif")
    out.append('#include "plcrt.h"')
    for inc in local_includes:
        out.append(f"#include {inc}")
    out.append("")
    if globals_:
        out.extend(globals_)
        out.append("")
    out.append("// ================================================================ block types")
    for bdef in used_types:
        out.append(_struct(bdef, "model"))
        out.append("")

    out.append("// ================================================================ program")
    out.append("struct Program {")
    for bid in order:
        r = resolved[bid]
        tmpl = ""
        if r.bdef.type_params:
            tmpl = "<" + ", ".join(T.C_TYPES[r.type_args[t]] for t in r.bdef.type_params) + ">"
        comment = f"  // {r.inst.comment}" if r.inst.comment else ""
        out.append(f"  FB_{r.bdef.type}{tmpl} {r.inst.name};{comment}")
    out.append("")
    out.append("  void fbd_init(const double* p) {")
    param_of = {(q["block"], q["port"]): i for i, q in enumerate(params)}
    for bid in order:
        r = resolved[bid]
        out.append(f"    // {r.inst.name} ({r.bdef.type})")
        for name, val in param_values[bid].items():
            out.append(f"    {r.inst.name}.{name} = {val};")
        for base, n in r.counts.items():
            out.append(f"    {r.inst.name}.{base}_n = {n};")
        for pname, lit in const_values[bid].items():
            port = r.inputs[pname]
            k = param_of.get((bid, pname))
            val = lit if k is None else _model_from_double(port.type, f"p[{k}]")
            out.append(f"    {r.inst.name}.{port.member()} = {val};")
        out.append(f"    {r.inst.name}.init();")
    out.append("  }")
    out.append("")
    out.append("  void fbd_step(const double* u, double* y) {")
    in_of = {(q["block"], q["port"]): i for i, q in enumerate(inputs)}
    out_of = {(q["block"], q["port"]): i for i, q in enumerate(outputs)}
    for n, bid in enumerate(order, 1):
        r = resolved[bid]
        out.append(f"    // [{n}] {r.inst.name} ({r.bdef.type})")
        for pname, port in r.inputs.items():
            src = wired.get((bid, pname))
            if not src:
                continue
            sres, sport, wid = src
            expr = f"{sres.inst.name}.{sport.member()}"
            if sport.type != port.type:
                expr = f"({T.C_TYPES[port.type]}){expr}"
            fb = "  // feedback: value from previous scan" if wid in feedback else ""
            out.append(f"    {r.inst.name}.{port.member()} = {expr};{fb}")
        for pname, port in r.outputs.items():
            k = in_of.get((bid, pname))
            if k is not None:
                out.append(f"    {r.inst.name}.{port.member()} = {_model_from_double(port.type, f'u[{k}]')};  // from Modelica")
        out.append(f"    {r.inst.name}.cycle();")
        for pname, port in r.inputs.items():
            k = out_of.get((bid, pname))
            if k is not None:
                out.append(f"    y[{k}] = {_model_to_double(port.type, f'{r.inst.name}.{port.member()}')};  // to Modelica")
    out.append("  }")
    out.append("};")
    out.append("}  // namespace")
    out.append("")
    out.append("#if defined(_WIN32)")
    out.append('#define FBD_API extern "C" __declspec(dllexport)')
    out.append("#else")
    out.append('#define FBD_API extern "C" __attribute__((visibility("default")))')
    out.append("#endif")
    out.append("")
    out.append(f"static const int FBD_NU = {len(inputs)}, FBD_NY = {len(outputs)}, FBD_NP = {len(params)};")
    out.append("")
    out.append("static void fbd_set_time(double t) { fbd_now_us = t > 0 ? (uint64_t)(t * 1e6 + 0.5) : 0; }")
    out.append("")
    out.append("FBD_API void* @@ID@@_new(double cycle_s, const double* p, int np) {")
    out.append("  (void)cycle_s;  // the scan cycle is the Modelica sample period")
    out.append("  double pv[FBD_NP + 1] = {0};")
    out.append("  for (int i = 0; i < FBD_NP && i < np; i++) pv[i] = p[i];")
    out.append("  void* mem = malloc(sizeof(Program));")
    out.append("  if (!mem) return 0;")
    out.append("  Program* prog = new (mem) Program();")
    out.append("  fbd_set_time(0);")
    out.append("  prog->fbd_init(pv);")
    out.append("  return prog;")
    out.append("}")
    out.append("")
    out.append("FBD_API void @@ID@@_free(void* h) {")
    out.append("  if (!h) return;")
    out.append("  ((Program*)h)->~Program();")
    out.append("  free(h);")
    out.append("}")
    out.append("")
    out.append("FBD_API void @@ID@@_step(void* h, double t, const double* u, int nu, double* y, int ny) {")
    out.append("  double uv[FBD_NU + 1] = {0}, yv[FBD_NY + 1] = {0};")
    out.append("  for (int i = 0; i < FBD_NU && i < nu; i++) uv[i] = u[i];")
    out.append("  fbd_set_time(t);")
    out.append("  if (h) ((Program*)h)->fbd_step(uv, yv);")
    out.append("  for (int i = 0; i < ny; i++) y[i] = i < FBD_NY ? yv[i] : 0.0;")
    out.append("}")
    out.append("")
    res.code = "\n".join(out)
    for lst in (inputs, outputs, params):
        for q in lst:
            q.pop("c", None)
    res.model = {"inputs": inputs, "outputs": outputs, "params": params}
