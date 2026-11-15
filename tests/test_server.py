import io
import time
import zipfile

from fastapi.testclient import TestClient

from fbdstudio.server import create_app


def make_client(tmp_path):
    return TestClient(create_app(tmp_path / "ws"))


def test_library_projects_compile_export(tmp_path):
    with make_client(tmp_path) as c:
        lib = c.get("/api/library").json()
        types = {b["type"] for b in lib["blocks"]}
        assert {"TON", "AND", "DI", "HYST"} <= types  # HYST = example user block copied to workspace
        assert not lib["errors"]

        projects = c.get("/api/projects").json()["projects"]
        examples = [p for p in projects if p["source"] == "example"]
        assert len(examples) >= 23
        for ex in examples:
            r = c.get(f"/api/projects/example/{ex['name']}")
            assert r.status_code == 200, f"example '{ex['name']}' cannot be opened (file name characters?)"
            proj = r.json()
            assert proj["name"] == ex["name"]
            for target in ("sim", "device"):
                res = c.post("/api/compile", json={"project": proj, "target": target}).json()
                assert res["ok"], (ex["name"], target, res["diagnostics"])

        proj["name"] = "My test"
        assert c.put("/api/projects/My test", json=proj).status_code == 200
        assert any(p["name"] == "My test" for p in c.get("/api/projects").json()["projects"])
        assert c.get("/api/projects/workspace/My test").json()["name"] == "My test"

        r = c.post("/api/export", json={"project": proj})
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = set(z.namelist())
        assert {"My_test/platformio.ini", "My_test/src/main.cpp", "My_test/include/plcrt.h"} <= names
        assert "board = esp32dev" in z.read("My_test/platformio.ini").decode()

        r = c.post("/api/export/modelica", json={"project": proj})
        assert r.status_code == 200, r.text
        assert 'filename="MyTest.zip"' in r.headers["content-disposition"]
        z = zipfile.ZipFile(io.BytesIO(r.content))
        assert "MyTest/package.mo" in z.namelist()
        assert any(n.startswith("MyTest/Resources/Library/") and n.endswith("libfbd_MyTest.a") for n in z.namelist())

        assert c.delete("/api/projects/My test").status_code == 200


def test_user_block_errors_are_reported(tmp_path):
    with make_client(tmp_path) as c:
        (tmp_path / "ws" / "blocks" / "bad.yaml").write_text("type: 1BAD\ninputs: []\n")
        lib = c.post("/api/library/reload").json()
        assert any("bad.yaml" in e for e in lib["errors"])


def test_sim_over_http_and_websocket(tmp_path):
    with make_client(tmp_path) as c:
        proj = c.get("/api/projects/example/01 Blink").json()
        res = c.post("/api/sim/start", json={"project": proj}).json()
        assert res["started"], res
        try:
            with c.websocket_connect("/ws") as ws:
                seen = set()
                end = time.time() + 5
                while time.time() < end and "values" not in seen:
                    seen.add(ws.receive_json()["ev"])
                assert "values" in seen
        finally:
            c.post("/api/link/stop")


def test_scope_capture_over_http_and_websocket(tmp_path):
    with make_client(tmp_path) as c:
        assert c.post("/api/capture", json={"channels": [0]}).status_code == 409  # not online
        proj = c.get("/api/projects/example/21 Filter lab").json()
        assert len(proj["trend"]) == 4  # the example comes with plotted signals
        res = c.post("/api/sim/start", json={"project": proj}).json()
        assert res["started"], res
        try:
            syms = res["compile"]["symbols"]
            ids = {b["name"]: b["id"] for b in proj["blocks"]}
            idx = next(s["idx"] for s in syms if s["block"] == ids["LowPass"] and s["port"] == "OUT")
            assert c.post("/api/capture", json={"channels": [9999]}).status_code == 400
            with c.websocket_connect("/ws") as ws:
                r = c.post("/api/capture", json={"channels": [idx], "length": 100, "pre": 10, "trigger": idx, "edge": 1, "level": 50})
                assert r.status_code == 200
                states, cap = [], None
                end = time.time() + 10
                while time.time() < end and cap is None:
                    ev = ws.receive_json()
                    if ev["ev"] == "capture_state":
                        states.append(ev["state"])
                    elif ev["ev"] == "capture":
                        cap = ev
                assert cap, states
                assert states[:2] == ["armed", "triggered"] and "receiving" in states
                assert len(cap["data"][0]) == 100 and cap["pre"] == 10 and cap["channels"] == [idx]
                assert cap["data"][0][9] < 50 <= cap["data"][0][10]
        finally:
            c.post("/api/link/stop")


def test_project_keeps_scope_signals(tmp_path):
    with make_client(tmp_path) as c:
        proj = c.get("/api/projects/example/19 PID tuning with a simulated plant").json()
        proj["name"] = "Scope test"
        proj["trend"] = proj["trend"][:1]
        assert c.put("/api/projects/Scope test", json=proj).status_code == 200
        assert c.get("/api/projects/workspace/Scope test").json()["trend"] == proj["trend"]
