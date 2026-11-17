"""Connections to a running program: a real board over serial, or the simulator.

Both speak the same line protocol (see runtime/plcrt.h). Lines are parsed here
and handed to a callback as event dicts.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from . import types as T

EventCb = Callable[[dict], None]


def parse_value(type_name: str, text: str):
    if type_name == "BOOL":
        return text == "1"
    if type_name == "REAL":
        try:
            v = float(text)
        except ValueError:
            return None
        return v if math.isfinite(v) else None
    try:
        return int(text)
    except ValueError:
        return None


def parse_line(line: str, symbols: list[dict]) -> dict:
    line = line.rstrip("\r\n")
    if line.startswith("="):
        parts = line[1:].split()
        try:
            t, scan, scan_max = (int(x) for x in parts[:3])
        except ValueError:
            return {"ev": "serial", "text": line}
        raw = parts[3:]
        if len(raw) != len(symbols):
            return {"ev": "mismatch", "text": f"device sent {len(raw)} values, program has {len(symbols)}"}
        values = [parse_value(s["type"], v) for s, v in zip(symbols, raw)]
        return {"ev": "values", "t": t, "scan_us": scan, "scan_max_us": scan_max, "values": values}
    if line.startswith("#FBD "):
        parts = line.split()
        num = lambda i: int(parts[i]) if len(parts) > i and parts[i].isdigit() else None
        return {"ev": "hello", "hash": parts[2] if len(parts) > 2 else "", "count": num(3),
                "restored": num(4)}  # None: firmware from before saved values existed
    if line.startswith("!W "):
        return {"ev": "ack", "idx": line[3:].strip()}
    if line.startswith("!S "):
        n = line[3:].strip()
        return {"ev": "saved", "count": int(n) if n.isdigit() else None}
    if line.strip() == "!X":
        return {"ev": "cleared"}
    if line.startswith("!E "):
        return {"ev": "device_error", "text": line[3:]}
    return {"ev": "serial", "text": line}


def format_write(type_name: str, value) -> str:
    v = T.parse_value(type_name, value)
    if type_name == "BOOL":
        return "1" if v else "0"
    if type_name == "REAL":
        return repr(float(v))
    return str(int(v))


class Link:
    kind = "none"

    def __init__(self, symbols: list[dict], expected_hash: str, on_event: EventCb):
        self.symbols = symbols
        self.expected_hash = expected_hash
        self.on_event = on_event
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._capture: dict | None = None  # capture data being received
        self._capture_channels: list[int] = []

    def _capture_line(self, line: str) -> None:
        """Capture (scope) protocol lines: %A armed, %T triggered, %C header, %D sample, %E end."""
        tag, rest = line[:2], line[2:].split()
        try:
            if tag == "%A":
                self.on_event({"ev": "capture_state", "state": "armed", "len": int(rest[0]), "channels": int(rest[1])})
            elif tag == "%T":
                self.on_event({"ev": "capture_state", "state": "triggered"})
            elif tag == "%C":
                n, nch, period, pre = (int(x) for x in rest[:4])
                self._capture = {"len": n, "period_us": period, "pre": pre, "data": [[] for _ in range(nch)]}
                self.on_event({"ev": "capture_state", "state": "receiving", "len": n})
            elif tag == "%D" and self._capture:
                for col, v in zip(self._capture["data"], rest):
                    f = float(v)
                    col.append(f if math.isfinite(f) else None)
            elif tag == "%E" and self._capture:
                cap, self._capture = self._capture, None
                cap["channels"] = self._capture_channels
                self.on_event({"ev": "capture", **cap})
        except (ValueError, IndexError):
            self.on_event({"ev": "serial", "text": line})

    def start_capture(self, channels: list[int], every: int = 1, length: int = 500, pre: int = 0,
                      trigger: int = -1, edge: int = 1, level: float = 0.5) -> None:
        if not channels or len(channels) > 8:
            raise ValueError("choose 1 to 8 signals")
        for i in [*channels, *([trigger] if trigger >= 0 else [])]:
            if not 0 <= i < len(self.symbols):
                raise ValueError(f"no signal with index {i}")
        if not math.isfinite(level):
            raise ValueError("the trigger level must be a number")
        self._capture, self._capture_channels = None, list(channels)
        self.send(f"C {int(every)} {int(length)} {int(pre)} {int(trigger)} {int(edge)} {float(level)!r} "
                  + " ".join(str(int(c)) for c in channels))

    def cancel_capture(self) -> None:
        self._capture = None
        self.send("C")

    def _emit_line(self, line: str) -> None:
        if not line.strip():
            return  # blank lines (e.g. from CR/LF differences) carry no information
        if line.startswith("%"):
            self._capture_line(line.rstrip("\r\n"))
            return
        ev = parse_line(line, self.symbols)
        if ev["ev"] == "hello":
            ev["match"] = ev["hash"] == self.expected_hash
        self.on_event(ev)

    def send(self, line: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        self._stop.set()

    def write_symbol(self, idx: int, value) -> None:
        sym = self.symbols[idx]
        if not sym["writable"]:
            raise ValueError("this value cannot be written (it is driven by a wire)")
        self.send(f"W {idx} {format_write(sym['type'], value)}")

    def save_values(self) -> None:
        """Store the current values of all writable symbols on the controller (answer: 'saved')."""
        self.send("S")

    def clear_saved_values(self) -> None:
        """Forget the saved values; the project's values apply after the next restart (answer: 'cleared')."""
        self.send("X")


class SimLink(Link):
    kind = "sim"

    def __init__(self, exe: Path, symbols, expected_hash, on_event, values_file: Path | None = None):
        """values_file: where the simulated controller keeps its saved values (None: it cannot save)."""
        super().__init__(symbols, expected_hash, on_event)
        env = dict(os.environ)
        env.pop("FBD_PARAMS_FILE", None)
        if values_file is not None:
            Path(values_file).parent.mkdir(parents=True, exist_ok=True)
            env["FBD_PARAMS_FILE"] = str(values_file)
        self.proc = subprocess.Popen(
            [str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env,
        )
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            if self._stop.is_set():
                break
            self._emit_line(line)
        if not self._stop.is_set():
            self.on_event({"ev": "link", "state": "stopped", "kind": self.kind,
                           "text": f"Simulator exited (code {self.proc.poll()})"})

    def send(self, line: str) -> None:
        with self._lock:
            if self.proc.poll() is None and self.proc.stdin:
                try:
                    self.proc.stdin.write(line + "\n")
                    self.proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass

    def set_pin(self, kind: str, pin: int, value: int) -> None:
        if kind not in ("D", "A"):
            raise ValueError("kind must be D or A")
        self.send(f"{kind} {int(pin)} {int(value)}")

    def close(self) -> None:
        super().close()
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()


def is_garbled(text: str) -> bool:
    """True for bytes received at the wrong baud rate (e.g. the ESP32 boot ROM's 74880-baud banner)."""
    bad = sum(1 for c in text if c == "\ufffd" or (ord(c) < 32 and c != "\t") or 127 <= ord(c) < 160)
    return bad > 0 and bad * 8 >= len(text) or bad >= 3


class SerialLink(Link):
    kind = "serial"
    _in_garbage = False

    def _emit_line(self, line: str) -> None:
        line = line.rstrip("\r\n")
        if line.strip() and is_garbled(line):
            # One note per burst instead of a screen of noise.
            if not self._in_garbage:
                self._in_garbage = True
                self.on_event({"ev": "serial", "text": "(board restarted - its boot messages use 74880 baud and are not shown)"})
            return
        if line.strip():
            self._in_garbage = False
        super()._emit_line(line)

    def __init__(self, port: str, baud: int, symbols, expected_hash, on_event):
        super().__init__(symbols, expected_hash, on_event)
        import serial  # pyserial

        self.port = port
        ser = serial.Serial()
        ser.port = port
        ser.baudrate = baud
        ser.timeout = 0.1
        # Connect without resetting the board. On ESP32 (and similar) boards DTR/RTS
        # drive EN and GPIO0 through the auto-reset circuit, and EN is pulled low
        # while RTS is on and DTR is off.
        if sys.platform == "win32":
            # Windows applies both line states in one call when the port opens.
            ser.dtr = False
            ser.rts = False
            ser.open()
        else:
            # Linux/macOS turn both lines on when the port opens, and pyserial would
            # then switch DTR off before RTS: a short reset pulse. Switch RTS off
            # first instead (DTR on/RTS off only touches GPIO0, which is harmless
            # while the chip runs), then DTR.
            ser.open()
            ser.rts = False
            ser.dtr = False
        self.ser = ser
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        self.send("?")

    def _reader(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self.ser.read(256)
            except Exception as e:  # unplugged
                if not self._stop.is_set():
                    self.on_event({"ev": "link", "state": "stopped", "kind": self.kind, "text": f"Serial error: {e}"})
                return
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self._emit_line(raw.decode("utf-8", errors="replace"))
            if len(buf) > 8192:
                buf = b""

    def send(self, line: str) -> None:
        with self._lock:
            try:
                self.ser.write((line + "\n").encode())
            except Exception:
                pass

    def close(self) -> None:
        super().close()
        if self._thread:
            self._thread.join(timeout=1)
        try:
            self.ser.close()
        except Exception:
            pass


def list_serial_ports() -> list[dict]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    ports = []
    for p in list_ports.comports():
        # Linux lists every legacy /dev/ttyS* UART even when nothing is behind it;
        # those have no hardware id. Boards (USB serial adapters) always have one.
        if p.device.startswith("/dev/ttyS") and (not p.hwid or p.hwid == "n/a"):
            continue
        ports.append({"device": p.device, "description": p.description or "", "hwid": p.hwid or ""})
    return sorted(ports, key=lambda p: ("USB" not in p["hwid"], p["device"]))
