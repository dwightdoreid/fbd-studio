"""Board definitions: PlatformIO settings and pin capabilities."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, ValidationError

BUILTIN_DIR = Path(__file__).parent / "library" / "boards"


class PinDef(BaseModel):
    id: str  # name shown to the user, e.g. "GPIO2" or "PA5"
    c: str  # C expression used in generated code, e.g. "2" or "PA5"
    caps: list[str]
    note: str = ""
    warn: bool = False  # show the note as a warning when the pin is used


class Restriction(BaseModel):
    """Pins that cannot be used for a capability while a board feature is in use."""
    feature: str
    capability: str
    pins: list[str]
    message: str
    level: str = "error"


class BoardDef(BaseModel):
    id: str
    title: str
    description: str = ""
    platformio: dict[str, str]
    adc_max: int = 1023
    build_flags: list[str] = Field(default_factory=list)
    pins: list[PinDef]
    features: list[str] = Field(default_factory=list)  # e.g. i2c, spi, wifi, bt_classic
    pin_defaults: dict[str, str] = Field(default_factory=dict)  # e.g. sda: GPIO21 (used by "@sda" defaults)
    restrictions: list[Restriction] = Field(default_factory=list)
    source: str = "builtin"

    def pin(self, pin_id: str) -> Optional[PinDef]:
        return next((p for p in self.pins if p.id == pin_id), None)

    def pin_index(self, pin_id: str) -> int:
        return next(i for i, p in enumerate(self.pins) if p.id == pin_id)


class Boards:
    def __init__(self, user_dirs: list[Path] | None = None):
        self.user_dirs = [Path(d) for d in (user_dirs or [])]
        self.boards: dict[str, BoardDef] = {}
        self.errors: list[str] = []
        self.reload()

    def reload(self) -> None:
        self.boards, self.errors = {}, []
        for d, builtin in [(BUILTIN_DIR, True)] + [(d, False) for d in self.user_dirs]:
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.yaml")):
                try:
                    data = yaml.safe_load(f.read_text(encoding="utf-8"))
                    board = BoardDef(**{**data, "source": "builtin" if builtin else str(f)})
                except (yaml.YAMLError, ValidationError, TypeError) as e:
                    self.errors.append(f"{f.name}: {e}")
                    continue
                if board.id in self.boards:
                    self.errors.append(f"{f.name}: board '{board.id}' already defined, skipped")
                    continue
                self.boards[board.id] = board

    def get(self, board_id: str) -> Optional[BoardDef]:
        return self.boards.get(board_id)

    def to_json(self) -> dict:
        return {"boards": [b.model_dump() for b in self.boards.values()], "errors": self.errors}
