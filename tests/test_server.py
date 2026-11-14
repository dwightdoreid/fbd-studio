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
        assert len(examples) >= 3
        for ex in examples:
            proj = c.get(f"/api/projects/example/{ex['name']}").json()
            res = c.post("/api/compile", json={"project": proj}).json()
            assert res["ok"], res["diagnostics"]

        proj["name"] = "My test"
        assert c.put("/api/projects/My test", json=proj).status_code == 200
        assert any(p["name"] == "My test" for p in c.get("/api/projects").json()["projects"])
        assert c.get("/api/projects/workspace/My test").json()["name"] == "My test"

        r = c.post("/api/export", json={"project": proj})
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = set(z.namelist())
        assert {"My_test/platformio.ini", "My_test/src/main.cpp", "My_test/include/plcrt.h"} <= names
        assert "board = esp32dev" in z.read("My_test/platformio.ini").decode()

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
