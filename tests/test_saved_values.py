"""Saving online-changed values on the controller ("Save values"), tested in the simulator."""

import queue

from fbdstudio import builder
from fbdstudio.compiler import compile_project
from fbdstudio.link import SimLink, parse_line
from fbdstudio.project import Project

from test_compiler import blk, errors, wait_for


PID = "20261117-120000-abc123"


def program(pv="5", pt="T#1s", project_id=PID):
    return Project(id=project_id, name="saved", settings={"cycle_ms": 5, "monitor_ms": 20}, blocks=[
        blk("ctu", "CTU", name="Count", values={"PV": pv}),
        blk("ton", "TON", x=200, name="Delay", values={"PT": pt}),
        blk("mv", "MOVE", x=400, name="Gain", type_args={"T": "REAL"}, values={"IN": "1.5"}),
        blk("and", "AND", x=600, name="Gate"),
    ])


def start(library, board, tmp_path, project, values_file):
    res = compile_project(project, library, board, target="sim")
    assert res.ok, errors(res)
    exe = builder.build_sim(res, tmp_path / "cache")
    events = queue.Queue()
    link = SimLink(exe, res.symbols, res.runtime_hash, events.put, values_file)
    sym = {(s["block"], s["port"]): s["idx"] for s in res.symbols}
    hello = wait_for(events, lambda e: e["ev"] == "hello")
    return link, events, sym, hello


def values(events):
    return wait_for(events, lambda e: e["ev"] == "values")["values"]


def test_saved_values_survive_a_restart(library, board, tmp_path):
    vf = tmp_path / "values.bin"
    link, ev, sym, hello = start(library, board, tmp_path, program(), vf)
    try:
        assert hello["restored"] == 0
        link.write_symbol(sym[("ctu", "PV")], 42)
        link.write_symbol(sym[("ton", "PT")], "T#2s500ms")
        link.write_symbol(sym[("mv", "IN")], -0.25)
        link.write_symbol(sym[("and", "IN1")], True)
        wait_for(ev, lambda e: e["ev"] == "values" and e["values"][sym[("and", "IN1")]] is True)
        link.save_values()
        saved = wait_for(ev, lambda e: e["ev"] in ("saved", "device_error"))
        assert saved == {"ev": "saved", "count": 8}  # all unconnected inputs: CTU CU/R/PV, TON IN/PT, MOVE IN, AND IN1/IN2
    finally:
        link.close()
    assert vf.exists()

    # restart ("power cycle"): the saved values replace the project's
    link, ev, sym, hello = start(library, board, tmp_path, program(), vf)
    try:
        assert hello["match"] and hello["restored"] == 8
        v = values(ev)
        assert v[sym[("ctu", "PV")]] == 42
        assert v[sym[("ton", "PT")]] == 2500
        assert v[sym[("mv", "IN")]] == -0.25
        assert v[sym[("and", "IN1")]] is True
    finally:
        link.close()


def test_changed_project_value_wins(library, board, tmp_path):
    vf = tmp_path / "values.bin"
    link, ev, sym, _ = start(library, board, tmp_path, program(), vf)
    try:
        link.write_symbol(sym[("ctu", "PV")], 42)
        link.write_symbol(sym[("ton", "PT")], 300)
        link.save_values()
        wait_for(ev, lambda e: e["ev"] == "saved")
    finally:
        link.close()

    # PV changed in the diagram and "uploaded" again: the new project value applies,
    # the untouched PT keeps its saved value; a newly added block does not disturb them.
    p = program(pv="7")
    p.blocks.append(blk("new", "TON", x=800, name="Extra"))
    link, ev, sym, hello = start(library, board, tmp_path, p, vf)
    try:
        assert hello["restored"] == 7  # all but PV; the new block has nothing saved
        v = values(ev)
        assert v[sym[("ctu", "PV")]] == 7
        assert v[sym[("ton", "PT")]] == 300
    finally:
        link.close()


def test_clear_saved_values(library, board, tmp_path):
    vf = tmp_path / "values.bin"
    link, ev, sym, _ = start(library, board, tmp_path, program(), vf)
    try:
        link.write_symbol(sym[("ctu", "PV")], 42)
        link.save_values()
        wait_for(ev, lambda e: e["ev"] == "saved")
        link.clear_saved_values()
        wait_for(ev, lambda e: e["ev"] == "cleared")
    finally:
        link.close()
    link, ev, sym, hello = start(library, board, tmp_path, program(), vf)
    try:
        assert hello["restored"] == 0
        assert values(ev)[sym[("ctu", "PV")]] == 5
    finally:
        link.close()


def test_damaged_file_is_ignored(library, board, tmp_path):
    vf = tmp_path / "values.bin"
    link, ev, sym, _ = start(library, board, tmp_path, program(), vf)
    try:
        link.write_symbol(sym[("ctu", "PV")], 42)
        link.save_values()
        wait_for(ev, lambda e: e["ev"] == "saved")
    finally:
        link.close()
    data = bytearray(vf.read_bytes())
    data[13] ^= 0xFF  # flip bits in a stored value: the checksum no longer matches
    vf.write_bytes(bytes(data))
    link, ev, sym, hello = start(library, board, tmp_path, program(), vf)
    try:
        assert hello["restored"] == 0
        assert values(ev)[sym[("ctu", "PV")]] == 5
    finally:
        link.close()


def test_without_storage_saving_reports_an_error(library, board, tmp_path):
    link, ev, _, hello = start(library, board, tmp_path, program(), None)
    try:
        assert hello["restored"] == 0
        link.save_values()
        err = wait_for(ev, lambda e: e["ev"] in ("saved", "device_error"))
        assert err["ev"] == "device_error" and "not supported" in err["text"]
    finally:
        link.close()


def test_protocol_lines():
    assert parse_line("#FBD 1 abcd1234 12 3", []) == {"ev": "hello", "hash": "abcd1234", "count": 12, "restored": 3}
    assert parse_line("#FBD 1 abcd1234 12", [])["restored"] is None  # older firmware
    assert parse_line("!S 4\r\n", []) == {"ev": "saved", "count": 4}
    assert parse_line("!X\r\n", []) == {"ev": "cleared"}


def test_generated_code_restores_before_init(library, board):
    res = compile_project(program(), library, board, target="device")
    assert res.ok, errors(res)
    code = res.code
    assert code.index("Count.PV = 5;") < code.index("plc::restore();") < code.index("Count.init();")
    assert all("key" not in s for s in res.symbols)


def test_server_endpoints_need_a_link(tmp_path):
    from fastapi.testclient import TestClient
    from fbdstudio.server import create_app
    with TestClient(create_app(tmp_path / "ws")) as c:
        assert c.post("/api/link/values/save").status_code == 409
        assert c.post("/api/link/values/clear").status_code == 409
        assert c.get("/static/js/app.js").headers["cache-control"] == "no-cache"
        assert c.get("/").headers["cache-control"] == "no-cache"


def save_pv(library, board, tmp_path, vf):
    link, ev, sym, _ = start(library, board, tmp_path, program(), vf)
    try:
        link.write_symbol(sym[("ctu", "PV")], 42)
        link.save_values()
        wait_for(ev, lambda e: e["ev"] == "saved")
    finally:
        link.close()


def test_other_project_does_not_use_saved_values(library, board, tmp_path):
    """Same blocks, ids, inputs and values, but another project: nothing is restored."""
    vf = tmp_path / "values.bin"
    save_pv(library, board, tmp_path, vf)
    link, ev, sym, hello = start(library, board, tmp_path, program(project_id="20261117-120000-zzz999"), vf)
    try:
        assert hello["restored"] == 0
        assert values(ev)[sym[("ctu", "PV")]] == 5
    finally:
        link.close()


def test_other_block_type_with_same_id_does_not_use_saved_values(library, board, tmp_path):
    vf = tmp_path / "values.bin"
    save_pv(library, board, tmp_path, vf)
    p = program()
    p.blocks[0] = blk("ctu", "CTD", name="Count", values={"PV": "5"})  # same id "ctu", input PV, INT, 5
    link, ev, sym, hello = start(library, board, tmp_path, p, vf)
    try:
        assert hello["restored"] == 5  # TON IN/PT, MOVE IN, AND IN1/IN2: none of the counter's inputs
        assert values(ev)[sym[("ctu", "PV")]] == 5
    finally:
        link.close()


def test_project_ids():
    import re
    from datetime import datetime, timezone
    from fbdstudio.project import new_project_id
    pid = new_project_id(datetime(2026, 11, 17, 14, 32, 5, tzinfo=timezone.utc))
    assert re.fullmatch(r"20261117-143205-[a-z0-9]{6}", pid)
    assert len({new_project_id() for _ in range(1000)}) == 1000
    import pydantic, pytest
    with pytest.raises(pydantic.ValidationError):
        Project(id="../evil")


def test_examples_have_unique_ids():
    import json
    from pathlib import Path
    ids = [json.loads(f.read_text())["id"] for f in Path("fbdstudio/examples/projects").glob("*.fbd.json")]
    assert len(ids) >= 23 and all(ids) and len(set(ids)) == len(ids)


def test_old_project_gets_an_id_once(tmp_path):
    import json
    from fastapi.testclient import TestClient
    from fbdstudio.server import create_app
    with TestClient(create_app(tmp_path / "ws")) as c:
        f = tmp_path / "ws" / "projects" / "Old.fbd.json"
        f.write_text(json.dumps({"version": 1, "name": "Old", "blocks": [], "wires": []}))
        first = c.get("/api/projects/workspace/Old").json()["id"]
        assert first and json.loads(f.read_text())["id"] == first
        assert c.get("/api/projects/workspace/Old").json()["id"] == first


def test_copy_board_values_into_project(tmp_path):
    """Online changes are read back exactly and listed as changes against the project."""
    import time
    from fastapi.testclient import TestClient
    from fbdstudio.server import create_app
    proj = program().model_dump()
    with TestClient(create_app(tmp_path / "ws")) as c:
        r = c.post("/api/sim/start", json={"project": proj}).json()
        assert r["started"], r
        sym = {(s["block"], s["port"]): s["idx"] for s in r["compile"]["symbols"]}
        try:
            assert c.post("/api/link/values/read", json={"project": proj}).json()["changes"] == []
            for (b, p), v in {("ctu", "PV"): 42, ("ton", "PT"): "T#1m30s", ("mv", "IN"): 0.1, ("and", "IN2"): False}.items():
                assert c.post("/api/link/write", json={"idx": sym[(b, p)], "value": v}).status_code == 200
            time.sleep(0.2)
            res = c.post("/api/link/values/read", json={"project": proj}).json()
            got = {(x["block"], x["port"]): (x["old"], x["new"]) for x in res["changes"]}
            assert got == {
                ("ctu", "PV"): ("5", "42"),
                ("ton", "PT"): ("T#1s", "T#1m30s"),
                ("mv", "IN"): ("1.5", "0.1"),  # exact, not the monitor's 4 decimals
                ("and", "IN2"): ("TRUE", "FALSE"),  # AND inputs default to TRUE
            }
            assert res["skipped"] == 0 and res["changes"][0]["name"] in {"Count", "Delay", "Gain", "Gate"}

            # after copying (the UI writes "new" into the block values) there is nothing left to copy
            for x in res["changes"]:
                next(b for b in proj["blocks"] if b["id"] == x["block"])["values"][x["port"]] = x["new"]
            assert c.post("/api/link/values/read", json={"project": proj}).json()["changes"] == []

            # an input removed from the diagram in the meantime is skipped
            proj["blocks"] = [b for b in proj["blocks"] if b["id"] != "and"]
            assert c.post("/api/link/values/read", json={"project": proj}).json()["skipped"] == 2
        finally:
            c.post("/api/link/stop")


def test_project_text_round_trips():
    from fbdstudio import types as T
    from fbdstudio.link import float32, parse_line, project_text
    for v in (0.1, 72.5, 1e-7, 3.0, -2.5e9, 1 / 3):
        text = project_text("REAL", float32(v))
        assert float32(T.parse_value("REAL", text)) == float32(v), text
    assert project_text("TIME", 90000) == "T#1m30s" and T.parse_time("T#1m30s") == 90000
    syms = [{"type": "INT", "writable": True}, {"type": "BOOL", "writable": False}, {"type": "REAL", "writable": True}]
    assert parse_line("!V 2 7 0.100000001", syms) == {"ev": "writable_values", "values": [7, 0.100000001]}
    assert parse_line("!V 3 7 1 2", syms)["ev"] == "mismatch"
