"""IEC 61131-3 style data types, literals and type compatibility rules."""

from __future__ import annotations

import math
import re

# Types that may appear on block ports (wires carry these).
PORT_TYPES = ["BOOL", "INT", "DINT", "REAL", "TIME"]

# C++ representation of every type usable in blocks (ports and internal vars).
C_TYPES = {
    "BOOL": "bool",
    "SINT": "int8_t",
    "INT": "int16_t",
    "DINT": "int32_t",
    "USINT": "uint8_t",
    "UINT": "uint16_t",
    "UDINT": "uint32_t",
    "REAL": "float",
    "TIME": "int32_t",  # milliseconds
}

# Type codes shared with the device runtime (plcrt.h) for monitoring.
TYPE_CODES = {"BOOL": 0, "INT": 1, "DINT": 2, "REAL": 3, "TIME": 4}

# Generic type groups usable as constraints for block type parameters.
TYPE_GROUPS = {
    "ANY": ["BOOL", "INT", "DINT", "REAL", "TIME"],
    "ANY_NUM": ["INT", "DINT", "REAL"],
    "ANY_INT": ["INT", "DINT"],
    "ANY_MAG": ["INT", "DINT", "REAL", "TIME"],
}

# Implicit (lossless) conversions allowed on a wire: source -> destination.
WIDENING = {("INT", "DINT"), ("INT", "REAL"), ("DINT", "REAL")}

INT_RANGES = {
    "SINT": (-128, 127),
    "INT": (-32768, 32767),
    "DINT": (-(2**31), 2**31 - 1),
    "USINT": (0, 255),
    "UINT": (0, 65535),
    "UDINT": (0, 2**32 - 1),
    "TIME": (-(2**31), 2**31 - 1),
}

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def expand_allowed(allowed: str | list[str]) -> list[str]:
    """Turn a type constraint ("ANY_NUM", "REAL" or a list) into concrete types."""
    items = [allowed] if isinstance(allowed, str) else list(allowed)
    out: list[str] = []
    for item in items:
        for t in TYPE_GROUPS.get(item, [item]):
            if t not in out:
                out.append(t)
    return out


def compatible(src: str, dst: str) -> bool:
    return src == dst or (src, dst) in WIDENING


# ---------------------------------------------------------------- TIME literals

_TIME_PREFIX = re.compile(r"^(?:T|TIME)#", re.IGNORECASE)
_TIME_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|d|h|m|s)", re.IGNORECASE)
_TIME_UNITS_MS = {"d": 86_400_000, "h": 3_600_000, "m": 60_000, "s": 1000, "ms": 1}


def parse_time(text: str | int | float) -> int:
    """Parse an IEC TIME literal (T#1m30s, TIME#250ms, t#1.5s) or plain ms number."""
    if isinstance(text, bool):
        raise ValueError("not a time value")
    if isinstance(text, (int, float)):
        return int(round(text))
    s = str(text).strip().replace("_", "")
    if not s:
        raise ValueError("empty time value")
    m = _TIME_PREFIX.match(s)
    if not m:
        try:
            return int(round(float(s)))
        except ValueError:
            raise ValueError(f"invalid TIME '{text}' (use e.g. T#1s500ms or a number of ms)")
    body = s[m.end():]
    neg = body.startswith("-")
    if neg:
        body = body[1:]
    pos, total = 0, 0.0
    for part in _TIME_PART.finditer(body):
        if part.start() != pos:
            break
        total += float(part.group(1)) * _TIME_UNITS_MS[part.group(2).lower()]
        pos = part.end()
    if pos != len(body) or pos == 0:
        raise ValueError(f"invalid TIME '{text}' (use e.g. T#1s500ms)")
    return -int(round(total)) if neg else int(round(total))


def format_time(ms: int) -> str:
    if ms == 0:
        return "T#0ms"
    sign = "-" if ms < 0 else ""
    ms = abs(ms)
    parts = []
    for unit, size in (("d", 86_400_000), ("h", 3_600_000), ("m", 60_000), ("s", 1000), ("ms", 1)):
        if ms >= size:
            parts.append(f"{ms // size}{unit}")
            ms %= size
    return f"T#{sign}{''.join(parts)}"


# ---------------------------------------------------------------- literals

def parse_value(type_name: str, value):
    """Validate a constant for the given type and return its Python value."""
    if type_name == "BOOL":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        s = str(value).strip().upper()
        if s in ("TRUE", "1", "ON"):
            return True
        if s in ("FALSE", "0", "OFF", ""):
            return False
        raise ValueError(f"invalid BOOL '{value}' (use TRUE or FALSE)")
    if type_name == "TIME":
        v = parse_time(value)
    elif type_name == "REAL":
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"invalid REAL '{value}'")
        if not math.isfinite(v):
            raise ValueError(f"REAL value must be finite, got '{value}'")
        return v
    elif type_name in INT_RANGES:
        if isinstance(value, bool):
            raise ValueError(f"invalid {type_name} '{value}'")
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"invalid {type_name} '{value}'")
        if f != int(f):
            raise ValueError(f"{type_name} must be a whole number, got '{value}'")
        v = int(f)
    else:
        raise ValueError(f"unknown type {type_name}")
    lo, hi = INT_RANGES[type_name]
    if not lo <= v <= hi:
        raise ValueError(f"{type_name} value {v} out of range {lo}..{hi}")
    return v


def c_literal(type_name: str, value) -> str:
    """C++ literal for a constant (value is parsed/validated first)."""
    v = parse_value(type_name, value)
    if type_name == "BOOL":
        return "true" if v else "false"
    if type_name == "REAL":
        r = repr(float(v))
        if "e" not in r and "." not in r:
            r += ".0"
        return r + "f"
    if type_name == "UDINT":
        return f"{v}UL"
    if type_name in ("DINT", "TIME") and v == -(2**31):
        return "(-2147483647L - 1)"
    return str(v)


def zero_value(type_name: str):
    return False if type_name == "BOOL" else 0.0 if type_name == "REAL" else 0


def c_string(text: str) -> str:
    """C++ string literal. Non-printable and non-ASCII bytes become octal escapes."""
    out = []
    for b in str(text).encode("utf-8"):
        ch = chr(b)
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ch == "?":
            out.append("\\?")  # no trigraphs
        elif 32 <= b < 127:
            out.append(ch)
        else:
            out.append(f"\\{b:03o}")
    return '"' + "".join(out) + '"'
