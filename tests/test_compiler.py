import os
import queue
import time

import pytest

from fbdstudio import builder, types as T
from fbdstudio.compiler import compile_project
from fbdstudio.link import SimLink
from fbdstudio.project import BlockInstance, PortRef, Project, Wire


def blk(id_, type_, x=0, y=0, **kw):
    return BlockInstance(id=id_, type=type_, name=kw.pop("name", f"{type_}_{id_}"), x=x, y=y, **kw)


def wire(id_, sb, sp, db, dp):
    return Wire(id=id_, src=PortRef(block=sb, port=sp), dst=PortRef(block=db, port=dp))


def errors(res):
    return [d.message for d in res.diagnostics if d.level == "error"]


def all_blocks_project(library, board, exclude=("BLE_UART",)):
    """One program instantiating every block with every allowed type argument.

    Blocks that cannot be combined (BT_SERIAL / BLE_UART) are left out via 'exclude'.
    """
    blocks = []
    pins = {"DI": "GPIO4", "DO": "GPIO16", "AI": "GPIO32", "PWM": "GPIO17"}
    n = 0
    for bdef in library.blocks.values():
        if bdef.type in exclude:
            continue
        combos = [{}]
        for tp, spec in bdef.type_params.items():
            combos = [dict(c, **{tp: t}) for c in combos for t in T.expand_allowed(spec.allowed)]
        for combo in combos:
            if bdef.singleton and len(combos) > 1 and combo != combos[0]:
                continue
            n += 1
            # bus/display pins keep their board defaults ("@sda" ...) where the board has them
            has_default = lambda p: str(p.default or "").startswith("@") and p.default[1:] in board.pin_defaults
            params = {p.name: pins[p.capability] for p in bdef.params if p.kind == "pin" and not has_default(p)}
            params.update({p.name: "x" for p in bdef.params if p.kind == "string" and p.required and not p.default})
            counts = {p.name: p.variadic.max for p in bdef.inputs if p.variadic}
            blocks.append(blk(f"b{n}", bdef.type, x=n, type_args=combo, params=params, counts=counts))
    # pins can only be used once when driven as an output: give every such pin parameter its own pin
    used = set(pins.values()) | set(board.pin_defaults.values())
    out_pins = iter(p.id for p in board.pins if "DO" in p.caps and "PWM" in p.caps and not p.warn and p.id not in used)
    for b in blocks:
        for pdef in library.get(b.type).params:
            no_default = not (str(pdef.default or "").startswith("@") and pdef.default[1:] in board.pin_defaults)
            if pdef.kind == "pin" and pdef.capability in ("DO", "PWM") and not pdef.shared and no_default:
                b.params[pdef.name] = next(out_pins)
    return Project(name="all", blocks=blocks)


def test_every_block_compiles_for_every_type(library, board, tmp_path):
    """Every built-in block with every allowed type, built for the simulator with g++ -std=c++11."""
    res = compile_project(all_blocks_project(library, board), library, board, target="sim")
    assert res.ok, errors(res)
    builder.build_sim(res, tmp_path)


def test_every_block_compiles_with_ble(library, board, tmp_path):
    res = compile_project(all_blocks_project(library, board, exclude=("BT_SERIAL",)), library, board, target="sim")
    assert res.ok, errors(res)
    builder.build_sim(res, tmp_path)


@pytest.mark.skipif(not os.environ.get("FBD_PIO_TESTS") or builder.find_pio() is None,
                    reason="set FBD_PIO_TESTS=1 (needs PlatformIO and the ESP32 toolchain; slow)")
@pytest.mark.parametrize("bluetooth", ["classic", "ble"])
def test_every_block_builds_for_esp32(library, board, tmp_path, bluetooth):
    """The same program built for the real ESP32 with PlatformIO (device code incl. I2C, SPI, WiFi, MQTT, BT/BLE)."""
    import subprocess
    exclude = ("BLE_UART",) if bluetooth == "classic" else ("BT_SERIAL",)
    project = all_blocks_project(library, board, exclude=exclude)
    for b in project.blocks:
        if b.type == "BLE_UART":
            b.params["passkey"] = 246810  # build the secured variant
    res = compile_project(project, library, board, target="device")
    assert res.ok, errors(res)
    out = builder.write_pio_project(tmp_path / "pio", board, res, 115200)
    proc = subprocess.run(builder.pio_command(out, upload=False, port=None), capture_output=True, text=True)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-6000:]


def test_type_mismatch_and_double_wire(library, board):
    p = Project(blocks=[blk("a", "TON"), blk("b", "ADD"), blk("c", "AND")], wires=[
        wire("w1", "a", "Q", "b", "IN1"),  # BOOL -> INT
        wire("w2", "a", "Q", "c", "IN1"),
        wire("w3", "a", "Q", "c", "IN1"),  # second wire to same input
    ])
    msgs = errors(compile_project(p, library, board))
    assert any("Type mismatch" in m for m in msgs)
    assert any("more than one wire" in m for m in msgs)


def test_widening_inserts_cast(library, board):
    p = Project(blocks=[blk("a", "MOVE", type_args={"T": "INT"}), blk("b", "MOVE", x=100, type_args={"T": "REAL"})],
                wires=[wire("w", "a", "OUT", "b", "IN")])
    res = compile_project(p, library, board)
    assert res.ok, errors(res)
    assert "MOVE_b.IN = (float)MOVE_a.OUT;" in res.code


def test_pin_checks(library, board):
    p = Project(blocks=[
        blk("a", "DO", params={"pin": "GPIO34"}),  # input-only pin
        blk("b", "DO", params={"pin": "GPIO16"}),
        blk("c", "DI", params={"pin": "GPIO16"}),  # conflicts with output
        blk("d", "AI"),  # no pin selected
    ])
    msgs = errors(compile_project(p, library, board))
    assert any("cannot be used as DO" in m for m in msgs)
    assert any("Pin GPIO16 is used by several blocks" in m for m in msgs)
    assert any("select a pin" in m for m in msgs)


def test_execution_order_and_feedback(library, board):
    # c is placed left of a, but data flows a -> b -> c, so order must follow data.
    p = Project(blocks=[blk("a", "NOT", x=300), blk("b", "NOT", x=200), blk("c", "NOT", x=0)],
                wires=[wire("w1", "a", "OUT", "b", "IN"), wire("w2", "b", "OUT", "c", "IN")])
    res = compile_project(p, library, board)
    assert res.order == ["a", "b", "c"] and res.feedback == []

    # a loop a -> b -> a: started at the leftmost block, wire into it is feedback
    p = Project(blocks=[blk("a", "NOT", x=0), blk("b", "NOT", x=200), blk("u", "NOT", x=-100)],
                wires=[wire("w1", "a", "OUT", "b", "IN"), wire("w2", "b", "OUT", "a", "IN")])
    res = compile_project(p, library, board)
    assert res.ok
    assert res.order == ["u", "a", "b"]
    assert res.feedback == ["w2"]


def test_invalid_names_and_values(library, board):
    p = Project(blocks=[blk("a", "TON", name="int"), blk("b", "TON", name="T1", values={"PT": "T#abc"}),
                        blk("c", "TON", name="T1"), blk("d", "NOPE", name="X")])
    msgs = errors(compile_project(p, library, board))
    assert any("not a valid instance name" in m for m in msgs)
    assert any("invalid TIME" in m for m in msgs)
    assert any("Duplicate instance name" in m for m in msgs)
    assert any("unknown block type" in m for m in msgs)


@pytest.fixture
def sim_events():
    return queue.Queue()


def wait_for(q, pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            ev = q.get(timeout=0.1)
        except queue.Empty:
            continue
        if pred(ev):
            return ev
    raise AssertionError("timed out waiting for event")


def test_simulation_end_to_end(library, board, tmp_path, sim_events):
    """DI -> TON(200ms) -> DO, plus a counter; drive pins and watch values through the monitor protocol."""
    p = Project(settings={"cycle_ms": 5, "monitor_ms": 20}, blocks=[
        blk("di", "DI", x=0, name="Button", params={"pin": "GPIO4"}),
        blk("ton", "TON", x=200, name="Delay", values={"PT": "T#200ms"}),
        blk("do", "DO", x=400, name="Lamp", params={"pin": "GPIO16"}),
        blk("ctu", "CTU", x=200, y=200, name="Count"),
    ], wires=[
        wire("w1", "di", "Q", "ton", "IN"),
        wire("w2", "ton", "Q", "do", "IN"),
        wire("w3", "di", "Q", "ctu", "CU"),
    ])
    res = compile_project(p, library, board, target="sim")
    assert res.ok, errors(res)
    exe = builder.build_sim(res, tmp_path)
    sym = {(s["block"], s["port"]): s["idx"] for s in res.symbols}
    link = SimLink(exe, res.symbols, res.runtime_hash, sim_events.put)
    try:
        hello = wait_for(sim_events, lambda e: e["ev"] == "hello")
        assert hello["match"]
        ev = wait_for(sim_events, lambda e: e["ev"] == "values")
        assert ev["values"][sym[("ton", "Q")]] is False

        link.set_pin("D", board.pin_index("GPIO4"), 1)
        wait_for(sim_events, lambda e: e["ev"] == "values" and e["values"][sym[("di", "Q")]] is True)
        t0 = time.time()
        wait_for(sim_events, lambda e: e["ev"] == "values" and e["values"][sym[("ton", "Q")]] is True)
        assert 0.1 < time.time() - t0 < 1.0
        assert wait_for(sim_events, lambda e: e["ev"] == "values")["values"][sym[("ctu", "CV")]] == 1

        # online change of an unconnected input (the counter preset)
        link.write_symbol(sym[("ctu", "PV")], 1)
        wait_for(sim_events, lambda e: e["ev"] == "ack")
        wait_for(sim_events, lambda e: e["ev"] == "values" and e["values"][sym[("ctu", "Q")]] is True)
    finally:
        link.close()
