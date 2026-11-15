"""Project (diagram) data model, stored as JSON files."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

FORMAT_VERSION = 1


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
    name: str = "Untitled"
    description: str = ""
    board: str = "esp32dev"
    settings: Settings = Field(default_factory=Settings)
    blocks: list[BlockInstance] = Field(default_factory=list)
    wires: list[Wire] = Field(default_factory=list)
    notes: list[Note] = Field(default_factory=list)
    trend: list[PortRef] = Field(default_factory=list)  # signals shown in the scope
