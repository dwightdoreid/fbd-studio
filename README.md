# FBD Studio

Graphical **function block diagram** programming for microcontrollers (ESP32
first, with STM32 and others planned). It is modelled on IEC 61131-3 PLC
programming and aimed at students, hobbyists and small industrial users.

- **PLC-style blocks:** AND/OR/XOR/NOT, SR/RS, R_TRIG/F_TRIG, TON/TOF/TP,
  CTU/CTD/CTUD, comparisons, arithmetic, SEL/MUX/LIMIT/MAX/MIN, type
  conversions, and DI/DO/AI/PWM I/O.
- **Communication blocks** where the board supports them: I2C (register
  read/write, bus scan), SPI transfer, WiFi, MQTT publish/subscribe (Home
  Assistant, Node-RED, …) and Bluetooth: classic serial for Android/PC, or
  BLE (Nordic UART Service, optional pairing PIN) for iPhone and Android.
- **Sensors and displays:** BME280/BMP280, DS18B20 (several per pin), DHT11/22,
  I2C character LCDs (16x2, 20x4), SSD1306/SH1106 OLEDs and ST7789 colour TFTs
  (incl. the built-in display of the ideaspark ESP32 1.14" board). Display lines are
  templates like `Temp {1:1} °C`. All drivers work step by step, so a slow
  sensor never stalls the scan cycle. MQTT runs in a
  background task, so a slow network never stalls the scan cycle.
- **Scan cycle semantics:** a fixed cycle time, data-flow execution order (shown
  on every block), and feedback loops that use the previous scan's value.
- **Simulator:** runs the *same generated C++* on your PC. Toggle inputs, move
  analog sliders and watch values live, with no hardware needed.
- **Build & upload** through PlatformIO, then **monitor online** over USB
  serial. You can change setpoints and timer presets while the board runs.
- **Readable code:** export the generated PlatformIO project and continue in
  C++ if you need to.
- **Extensible:** your own blocks and boards are simple YAML files with C++
  snippets. See [docs/BLOCKS.md](docs/BLOCKS.md).

## Quick start

```bash
./setup.sh                 # once: creates .venv and installs everything (safe to re-run)
./run.sh                   # starts FBD Studio and opens http://127.0.0.1:8765/
```

On **Windows** use `setup.bat` and `run.bat` instead (double-clicking them
works too). The simulator needs `g++`; the setup script explains how to get
it via MSYS2 if it is missing.

The setup options are: `--platformio` (also install PlatformIO for real boards),
`--recreate` (rebuild the venv from scratch), `--no-dev` (skip test tools).
The run script works from any directory, runs the setup automatically if needed,
and passes options through, e.g. `./run.sh --port 9000 --no-browser`.

Requirements:

- Python 3.10+
- a C++ compiler (`g++` or `clang++`) for the simulator
- [PlatformIO](https://platformio.org/) (`pip install platformio`) to build
  for and upload to real boards. The first ESP32 build downloads the toolchain
  (several hundred MB).

Projects, your own blocks and build output live in a workspace folder
(default `~/FBDStudio`, change it with `--workspace`).

## How it works

```
diagram (JSON) ─► compiler.py ─► main.cpp + plcrt.h ─┬─► PlatformIO ─► board ─┐
                  (types, pins,                     │                         │ serial line protocol
                   order, loops)                    └─► g++ + sim shim ─► PC  ┤ (values / online writes)
                                                                              ▼
                                                                web UI (live values on the diagram)
```

| path | what |
|---|---|
| `fbdstudio/library/blocks/*.yaml` | built-in block definitions |
| `fbdstudio/library/boards/*.yaml` | board definitions (pins and capabilities) |
| `fbdstudio/compiler.py` | validation, execution order, C++ generation |
| `fbdstudio/runtime/plcrt.h` | device runtime: scan timing, monitor protocol |
| `fbdstudio/runtime/sim/` | Arduino API emulation for the simulator |
| `fbdstudio/server.py` | FastAPI REST + WebSocket server |
| `fbdstudio/static/` | web UI (plain ES modules, no build step) |

## Tests

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

The tests compile every built-in block with every allowed data type
(`g++ -std=c++11`). They also run a simulated program end to end: they press
an input, check the timer delay and change a preset online.

## Status

Version 0.1, the MVP. It has been tested in the simulator. Real ESP32
hardware builds still need a PlatformIO toolchain run on your machine.
