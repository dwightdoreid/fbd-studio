"""Block definitions and the block library loader.

Every block — built-in or user supplied — is described by a YAML file. See
docs/BLOCKS.md for the full format.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from . import types as T

BUILTIN_DIR = Path(__file__).parent / "library" / "blocks"

RESERVED_FIELDS = {"init", "cycle"}


class Variadic(BaseModel):
    min: int = 2
    max: int = 8
    default: int = 2
    start: int = 1  # number of the first port, e.g. IN1 (start=1) or IN0 (start=0)

    @model_validator(mode="after")
    def _check(self):
        if not (1 <= self.min <= self.default <= self.max <= 32):
            raise ValueError("variadic requires 1 <= min <= default <= max <= 32")
        return self


class PortDef(BaseModel):
    name: str
    type: str
    default: Any = None
    description: str = ""
    variadic: Optional[Variadic] = None


class ParamDef(BaseModel):
    name: str
    kind: Literal["pin", "enum", "int", "real", "bool", "string"]
    capability: Optional[str] = None  # for pins: DI, DO, AI, PWM, I2C, SPI, ...
    shared: bool = False  # pin: may be shared with other shared uses of the same capability (e.g. SPI chip select)
    secret: bool = False  # string: hide the value in the UI (passwords)
    required: bool = False  # string: must not be empty
    hex: bool = False  # int: shown and entered as hexadecimal (e.g. I2C addresses)
    options: Optional[dict[str, str]] = None  # enum: label -> C expression
    default: Any = None
    min: Optional[float] = None
    max: Optional[float] = None
    description: str = ""


class VarDef(BaseModel):
    name: str
    type: str
    init: Any = None


class TypeParam(BaseModel):
    allowed: str | list[str] = "ANY"
    default: Optional[str] = None


class CodeOverride(BaseModel):
    includes: Optional[list[str]] = None
    globals: Optional[str] = None
    members: Optional[str] = None
    init: Optional[str] = None
    cycle: Optional[str] = None


class ModelIO(BaseModel):
    """How a hardware block appears in a Modelica export (see docs/BLOCKS.md).

    In the export the block's normal init/cycle code is replaced: 'from_model' outputs
    are set from Modelica inputs, 'to_model' inputs are sent to Modelica outputs, and the
    optional init/cycle code here runs instead (e.g. to set OK = true).
    """
    from_model: list[str] = Field(default_factory=list)  # block outputs that become Modelica inputs
    to_model: list[str] = Field(default_factory=list)  # block inputs that become Modelica outputs
    vars: list[VarDef] = Field(default_factory=list)  # extra state for the cycle code below
    init: str = ""
    cycle: str = ""


class BlockDef(BaseModel):
    type: str
    category: str = "User"
    title: str = ""
    description: str = ""
    color: Optional[str] = None
    inputs: list[PortDef] = Field(default_factory=list)
    outputs: list[PortDef] = Field(default_factory=list)
    vars: list[VarDef] = Field(default_factory=list)
    params: list[ParamDef] = Field(default_factory=list)
    type_params: dict[str, TypeParam] = Field(default_factory=dict)
    includes: list[str] = Field(default_factory=list)
    globals: str = ""
    members: str = ""  # extra C++ struct members, e.g. a driver object: "plc_dht::Sensor sensor;"
    init: str = ""
    cycle: str = ""
    lib_deps: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)  # board features, e.g. [wifi] ("a|b": one of them)
    requires_blocks: list[str] = Field(default_factory=list)  # block types that must be in the diagram ("A|B": one of them)
    excludes_blocks: list[str] = Field(default_factory=list)  # block types that cannot be in the same program
    singleton: bool = False  # at most one instance per program
    platformio: dict[str, str] = Field(default_factory=dict)  # extra platformio.ini options
    targets: dict[str, CodeOverride] = Field(default_factory=dict)
    model: Optional[ModelIO] = None  # Modelica export behaviour of hardware blocks
    source: str = "builtin"  # filled by the loader

    @model_validator(mode="after")
    def _check(self):
        if not T.IDENT_RE.match(self.type):
            raise ValueError(f"block type '{self.type}' must be a C identifier")
        seen: set[str] = set()
        for group in (self.inputs, self.outputs, self.vars, self.params):
            for item in group:
                if not T.IDENT_RE.match(item.name):
                    raise ValueError(f"'{item.name}' is not a valid identifier")
                if item.name in seen or item.name in RESERVED_FIELDS:
                    raise ValueError(f"duplicate or reserved name '{item.name}'")
                if item.name.endswith("_n"):
                    raise ValueError(f"'{item.name}': names ending in _n are reserved")
                seen.add(item.name)
        for tp_name, tp in self.type_params.items():
            if not T.IDENT_RE.match(tp_name) or tp_name in T.C_TYPES:
                raise ValueError(f"invalid type parameter name '{tp_name}'")
            allowed = T.expand_allowed(tp.allowed)
            bad = [a for a in allowed if a not in T.PORT_TYPES]
            if bad:
                raise ValueError(f"type parameter {tp_name}: unknown types {bad}")
            if tp.default is None:
                tp.default = allowed[0]
            if tp.default not in allowed:
                raise ValueError(f"type parameter {tp_name}: default {tp.default} not allowed")
        for p in self.inputs + self.outputs:
            if p.type not in T.PORT_TYPES and p.type not in self.type_params:
                raise ValueError(f"port {p.name}: unknown type '{p.type}'")
            if p.default is not None and p.type in T.PORT_TYPES:
                T.parse_value(p.type, p.default)
        for p in self.outputs:
            if p.variadic:
                raise ValueError(f"output {p.name}: only inputs can be variadic")
        for p in self.inputs:
            if p.variadic and p.name[-1].isdigit():
                raise ValueError(f"variadic input {p.name} must not end with a digit")
        for v in self.vars:
            if v.type not in T.C_TYPES and v.type not in self.type_params:
                raise ValueError(f"var {v.name}: unknown type '{v.type}'")
            if v.init is not None and v.type in T.C_TYPES:
                T.parse_value(v.type, v.init)
        if self.model:
            outs = {p.name for p in self.outputs}
            ins = {p.name: p for p in self.inputs}
            for name in self.model.from_model:
                if name not in outs:
                    raise ValueError(f"model.from_model: '{name}' is not an output")
            for name in self.model.to_model:
                if name not in ins or ins[name].variadic:
                    raise ValueError(f"model.to_model: '{name}' is not a (non-variadic) input")
        for p in self.params:
            if p.kind == "enum":
                if not p.options:
                    raise ValueError(f"enum param {p.name} needs options")
                if p.default is None:
                    p.default = next(iter(p.options))
                if p.default not in p.options:
                    raise ValueError(f"param {p.name}: default not in options")
            if p.kind == "string" and p.default is not None and not isinstance(p.default, str):
                p.default = str(p.default)
            if p.kind == "pin" and not p.capability:
                raise ValueError(f"pin param {p.name} needs a capability (e.g. DI)")
        return self

    def code_for(self, target: str) -> dict[str, Any]:
        if target == "model" and self.model is not None:
            return {"includes": [], "globals": "", "members": "", "init": self.model.init, "cycle": self.model.cycle}
        code = {"includes": self.includes, "globals": self.globals, "members": self.members,
                "init": self.init, "cycle": self.cycle}
        override = self.targets.get(target)
        if override:
            for key, val in override.model_dump().items():
                if val is not None:
                    code[key] = val
        return code


class Library:
    def __init__(self, user_dirs: list[Path] | None = None):
        self.user_dirs = [Path(d) for d in (user_dirs or [])]
        self.blocks: dict[str, BlockDef] = {}
        self.errors: list[str] = []
        self.reload()

    def reload(self) -> None:
        self.blocks = {}
        self.errors = []
        self._load_dir(BUILTIN_DIR, builtin=True)
        for d in self.user_dirs:
            if d.is_dir():
                self._load_dir(d, builtin=False)

    def _load_dir(self, directory: Path, builtin: bool) -> None:
        files = sorted(list(directory.glob("*.yaml")) + list(directory.glob("*.yml")))
        for f in files:
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
            except yaml.YAMLError as e:
                self.errors.append(f"{f.name}: YAML error: {e}")
                continue
            if data is None:
                continue
            entries = data.get("blocks") if isinstance(data, dict) and "blocks" in data else [data]
            if not isinstance(entries, list):
                self.errors.append(f"{f.name}: expected a block or a 'blocks:' list")
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    self.errors.append(f"{f.name}: each block must be a mapping")
                    continue
                name = entry.get("type", "?")
                try:
                    block = BlockDef(**{**entry, "source": "builtin" if builtin else str(f)})
                except ValidationError as e:
                    msgs = "; ".join(
                        f"{'.'.join(str(x) for x in err['loc']) or 'block'}: {err['msg']}" for err in e.errors()
                    )
                    self.errors.append(f"{f.name}: block '{name}': {msgs}")
                    continue
                except ValueError as e:
                    self.errors.append(f"{f.name}: block '{name}': {e}")
                    continue
                if block.type in self.blocks:
                    self.errors.append(f"{f.name}: block type '{block.type}' already defined, skipped")
                    continue
                self.blocks[block.type] = block

    def get(self, type_name: str) -> BlockDef | None:
        return self.blocks.get(type_name)

    def to_json(self) -> dict:
        return {
            "blocks": [b.model_dump() for b in self.blocks.values()],
            "errors": self.errors,
            "type_groups": T.TYPE_GROUPS,
            "widening": sorted([list(w) for w in T.WIDENING]),
        }
