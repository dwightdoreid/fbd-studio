"""FastAPI web server: REST API + WebSocket event stream + static web UI."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import builder, modelica
from .blocks import Library
from .boards import Boards
from .compiler import compile_project
from .link import Link, SerialLink, SimLink, list_serial_ports
from .project import Project

PKG = Path(__file__).parent
STATIC = PKG / "static"
EXAMPLES = PKG / "examples"
PROJECT_SUFFIX = ".fbd.json"


# ---------------------------------------------------------------- request bodies

class CompileReq(BaseModel):
    project: dict
    target: str = "device"


class BuildReq(BaseModel):
    project: dict
    upload: bool = False
    port: Optional[str] = None


class LinkReq(BaseModel):
    project: dict
    port: Optional[str] = None


class WriteReq(BaseModel):
    idx: int
    value: Any


class CaptureReq(BaseModel):
    channels: list[int]  # symbol indexes
    every: int = 1  # record every n-th scan
    length: int = 500  # samples
    pre: int = 0  # samples before the trigger
    trigger: int = -1  # symbol index, -1 = start at once
    edge: int = 1  # 1 rising, 2 falling, 3 both
    level: float = 0.5


class PinReq(BaseModel):
    kind: str
    pin: int
    value: int


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.projects = self.root / "projects"
        self.blocks = self.root / "blocks"
        self.boards = self.root / "boards"
        self.build = self.root / "build"
        self.sim_cache = self.root / ".sim"
        for d in (self.projects, self.build, self.sim_cache, self.boards):
            d.mkdir(parents=True, exist_ok=True)
        if not self.blocks.exists():
            # first run: give users an example of a custom block to learn from
            shutil.copytree(EXAMPLES / "blocks", self.blocks)


def safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9 _\-.]", "_", name).strip(" .")
    if not name:
        raise HTTPException(400, "Invalid project name")
    return name[:80]


class State:
    def __init__(self, ws_root: Path):
        self.ws = Workspace(ws_root)
        self.library = Library([self.ws.blocks])
        self.boards = Boards([self.ws.boards])
        self.clients: set[WebSocket] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.link: Optional[Link] = None
        self.link_info: dict = {}
        self.build_task: Optional[asyncio.Task] = None

    # ---- events to all browsers
    async def broadcast(self, ev: dict) -> None:
        msg = json.dumps(ev)
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                self.clients.discard(ws)

    def emit_threadsafe(self, ev: dict) -> None:
        if self.loop:
            asyncio.run_coroutine_threadsafe(self.broadcast(ev), self.loop)

    def stop_link(self) -> None:
        if self.link:
            self.link.close()
            self.link = None
            self.link_info = {}


def create_app(workspace: Path) -> FastAPI:
    state = State(workspace)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.loop = asyncio.get_running_loop()
        yield
        state.stop_link()

    app = FastAPI(title="FBD Studio", lifespan=lifespan)
    app.state.fbd = state

    # ------------------------------------------------------------- helpers
    def parse_project(data: Any) -> Project:
        try:
            return Project.model_validate(data)
        except ValidationError as e:
            raise HTTPException(400, f"Invalid project: {e}")

    def compile_(project: Project, target: str = "device"):
        board = state.boards.get(project.board)
        return compile_project(project, state.library, board, target), board

    # ------------------------------------------------------------- library
    @app.get("/api/library")
    def get_library():
        return {**state.library.to_json(), "user_blocks_dir": str(state.ws.blocks)}

    @app.post("/api/library/reload")
    def reload_library():
        state.library.reload()
        state.boards.reload()
        return {**state.library.to_json(), "user_blocks_dir": str(state.ws.blocks)}

    @app.get("/api/boards")
    def get_boards():
        return state.boards.to_json()

    # ------------------------------------------------------------- projects
    @app.get("/api/projects")
    def list_projects():
        items = []
        for f in sorted(state.ws.projects.glob(f"*{PROJECT_SUFFIX}")):
            items.append({"name": f.name[: -len(PROJECT_SUFFIX)], "source": "workspace", "modified": f.stat().st_mtime})
        for f in sorted((EXAMPLES / "projects").glob(f"*{PROJECT_SUFFIX}")):
            items.append({"name": f.name[: -len(PROJECT_SUFFIX)], "source": "example"})
        return {"projects": items, "dir": str(state.ws.projects)}

    def project_path(source: str, name: str) -> Path:
        base = state.ws.projects if source == "workspace" else EXAMPLES / "projects" if source == "example" else None
        if base is None:
            raise HTTPException(404, "Unknown source")
        return base / f"{safe_name(name)}{PROJECT_SUFFIX}"

    @app.get("/api/projects/{source}/{name}")
    def load_project(source: str, name: str):
        path = project_path(source, name)
        if not path.is_file():
            raise HTTPException(404, "Project not found")
        return parse_project(json.loads(path.read_text(encoding="utf-8"))).model_dump()

    @app.put("/api/projects/{name}")
    def save_project(name: str, data: dict):
        project = parse_project(data)
        path = project_path("workspace", name)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(project.model_dump(), indent=2), encoding="utf-8")
        tmp.replace(path)
        return {"saved": path.name}

    @app.delete("/api/projects/{name}")
    def delete_project(name: str):
        path = project_path("workspace", name)
        if path.is_file():
            path.unlink()
        return {"deleted": True}

    # ------------------------------------------------------------- compile / build
    @app.post("/api/compile")
    def api_compile(req: CompileReq):
        res, _ = compile_(parse_project(req.project), req.target)
        return res.to_json()

    @app.post("/api/export")
    def api_export(req: CompileReq):
        project = parse_project(req.project)
        res, board = compile_(project)
        if not res.ok:
            raise HTTPException(400, "Fix the errors in the diagram first")
        name = safe_name(project.name).replace(" ", "_")
        data = builder.zip_pio_project(name, board, res, project.settings.baud)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})

    @app.post("/api/export/modelica")
    def api_export_modelica(req: CompileReq):
        project = parse_project(req.project)
        res, _ = compile_(project, "model")
        if not res.ok:
            msgs = [d.message for d in res.diagnostics if d.level == "error"]
            raise HTTPException(400, "Cannot export to Modelica: " + "; ".join(msgs[:3]))
        try:
            pkg, data = modelica.zip_package(project, res)
        except RuntimeError as e:
            raise HTTPException(500, str(e))
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{pkg}.zip"'})

    @app.post("/api/build")
    async def api_build(req: BuildReq):
        if state.build_task and not state.build_task.done():
            raise HTTPException(409, "A build is already running")
        project = parse_project(req.project)
        res, board = compile_(project)
        if not res.ok:
            return {"started": False, "compile": res.to_json()}
        out_dir = state.ws.build / safe_name(project.name).replace(" ", "_")
        builder.write_pio_project(out_dir, board, res, project.settings.baud)
        try:
            cmd = builder.pio_command(out_dir, req.upload, req.port)
        except RuntimeError as e:
            return {"started": False, "error": str(e), "dir": str(out_dir), "compile": res.to_json()}
        if req.upload and state.link and state.link.kind == "serial":
            state.stop_link()  # the upload needs the serial port
            await state.broadcast({"ev": "link", "state": "stopped", "text": "Monitor stopped for upload"})
        state.build_task = asyncio.create_task(run_build(cmd, req.upload))
        return {"started": True, "dir": str(out_dir), "compile": res.to_json()}

    async def run_build(cmd: list[str], upload: bool) -> None:
        what = "Upload" if upload else "Build"
        await state.broadcast({"ev": "build", "line": "$ " + " ".join(cmd)})
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            assert proc.stdout
            async for raw in proc.stdout:
                await state.broadcast({"ev": "build", "line": raw.decode(errors="replace").rstrip()})
            code = await proc.wait()
        except Exception as e:  # noqa: BLE001
            await state.broadcast({"ev": "build", "line": f"{what} failed to start: {e}"})
            code = -1
        await state.broadcast({"ev": "build_done", "ok": code == 0, "upload": upload,
                               "text": f"{what} {'succeeded' if code == 0 else 'FAILED'}"})

    @app.get("/api/tools")
    def tools():
        return {"platformio": builder.find_pio(), "cxx": builder.find_cxx()}

    # ------------------------------------------------------------- online: sim + serial
    @app.get("/api/ports")
    def ports():
        return {"ports": list_serial_ports()}

    @app.post("/api/sim/start")
    async def sim_start(req: LinkReq):
        project = parse_project(req.project)
        res, _ = compile_(project, "sim")
        if not res.ok:
            return {"started": False, "compile": res.to_json()}
        try:
            exe = await asyncio.to_thread(builder.build_sim, res, state.ws.sim_cache)
        except RuntimeError as e:
            return {"started": False, "error": str(e), "compile": res.to_json()}
        state.stop_link()
        state.link = SimLink(exe, res.symbols, res.runtime_hash, state.emit_threadsafe)
        state.link_info = {"kind": "sim"}
        return {"started": True, "compile": res.to_json()}

    @app.post("/api/monitor/start")
    async def monitor_start(req: LinkReq):
        if not req.port:
            raise HTTPException(400, "Select a serial port")
        project = parse_project(req.project)
        res, _ = compile_(project)
        if not res.ok:
            return {"started": False, "compile": res.to_json()}
        state.stop_link()
        try:
            state.link = SerialLink(req.port, project.settings.baud, res.symbols, res.runtime_hash, state.emit_threadsafe)
        except Exception as e:  # noqa: BLE001
            return {"started": False, "error": f"Cannot open {req.port}: {e}", "compile": res.to_json()}
        state.link_info = {"kind": "serial", "port": req.port}
        return {"started": True, "compile": res.to_json()}

    @app.post("/api/link/stop")
    def link_stop():
        state.stop_link()
        return {"stopped": True}

    @app.post("/api/link/write")
    def link_write(req: WriteReq):
        if not state.link:
            raise HTTPException(409, "Not online")
        try:
            state.link.write_symbol(req.idx, req.value)
        except (ValueError, IndexError) as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.post("/api/capture")
    def capture_start(req: CaptureReq):
        if not state.link:
            raise HTTPException(409, "Not online: start the simulator or connect to the board first")
        try:
            state.link.start_capture(req.channels, req.every, req.length, req.pre, req.trigger, req.edge, req.level)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.post("/api/capture/cancel")
    def capture_cancel():
        if state.link:
            state.link.cancel_capture()
        return {"ok": True}

    @app.post("/api/sim/pin")
    def sim_pin(req: PinReq):
        if not isinstance(state.link, SimLink):
            raise HTTPException(409, "Simulator is not running")
        try:
            state.link.set_pin(req.kind, req.pin, req.value)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.get("/api/status")
    def status():
        return {"link": state.link_info if state.link else None,
                "building": bool(state.build_task and not state.build_task.done())}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        state.clients.add(ws)
        try:
            while True:
                await ws.receive_text()  # client -> server traffic goes through REST
        except WebSocketDisconnect:
            pass
        finally:
            state.clients.discard(ws)

    # ------------------------------------------------------------- UI
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
