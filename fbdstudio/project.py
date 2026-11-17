"""Project (diagram) data model, stored as JSON files."""

from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

FORMAT_VERSION = 1

_ID_CHARS = string.ascii_lowercase + string.digits


def new_project_id(now: datetime | None = None) -> str:
    """A project id: creation time (UTC) plus 6 random characters, e.g. 20261117-143205-k3x9q2.

    Values saved on a controller only apply to the program with the same project id
    (see "saved values" in runtime/plcrt.h), so two projects must never share one:
    the time makes ids of projects created at different seconds differ for certain,
    the random part covers projects created in the same second.
    """
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%d-%H%M%S-") + "".join(secrets.choice(_ID_CHARS) for _ in range(6))


class PortRef(BaseModel):
    block: str  # block instance id
    port: str  # port name, e.g. "Q" or "IN3" for variadic inputs


class Wire(BaseModel):
    id: str
    src: PortRef  # an output
    dst: PortRef  # an input


class BlockInstance(BaseModel):
    id: str
    type: str
    name: str  # instance name, used as C++ variable name
    x: float = 0
    y: float = 0
    values: dict[str, Any] = Field(default_factory=dict)  # constants for unconnected inputs
    params: dict[str, Any] = Field(default_factory=dict)
    type_args: dict[str, str] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)  # variadic input counts
    comment: str = ""


class Note(BaseModel):
    id: str
    x: float = 0
    y: float = 0
    text: str = ""


class Settings(BaseModel):
    cycle_ms: int = Field(10, ge=0, le=10000, description="Scan cycle time; 0 = run as fast as possible")
    monitor_ms: int = Field(200, ge=0, le=10000, description="Online monitoring update period")
    baud: int = 115200


class Project(BaseModel):
    version: int = FORMAT_VERSION
    # Identity of the program, kept across edits and renames of the diagram but new for
    # every new project or copy. "" only for projects from before ids existed (the
    # server gives them one when they are opened).
    id: str = Field("", max_length=64, pattern=r"^[A-Za-z0-9_\-]*$")
    name: str = "Untitled"
    description: str = ""
    board: str = "esp32dev"
    settings: Settings = Field(default_factory=Settings)
    blocks: list[BlockInstance] = Field(default_factory=list)
    wires: list[Wire] = Field(default_factory=list)
    notes: list[Note] = Field(default_factory=list)
    trend: list[PortRef] = Field(default_factory=list)  # signals shown in the scope
