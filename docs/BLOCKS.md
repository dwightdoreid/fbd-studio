# Writing your own blocks

Every block in FBD Studio, including all the built-in ones, is defined in a
YAML file. Your own blocks go in the **`blocks/` folder of your workspace**
(by default `~/FBDStudio/blocks/`). Press **Reload blocks** in the app after
you change a file. Problems in your files are shown at the top of the block
library.

The built-in definitions in `fbdstudio/library/blocks/` are good examples to
copy from.

## How a block becomes code

Each block type becomes a C++ `struct`. Its inputs, outputs, internal
variables and parameters are **member variables**, so your code uses them by
name:

```yaml
type: HYST
category: My blocks
title: Hysteresis switch
description: Q turns ON at HIGH_ and OFF at LOW_.
inputs:
  - {name: IN, type: REAL}
  - {name: HIGH_, type: REAL, default: 60}
  - {name: LOW_, type: REAL, default: 40}
outputs:
  - {name: Q, type: BOOL}
cycle: |
  if (IN >= HIGH_) Q = true;
  else if (IN <= LOW_) Q = false;
```

generates

```cpp
struct FB_HYST {
  float IN{};  float HIGH_{};  float LOW_{};   // inputs
  bool Q{};                                    // outputs
  void init() { }
  void cycle() {
    if (IN >= HIGH_) Q = true;
    else if (IN <= LOW_) Q = false;
  }
};
```

On every scan cycle the program copies the wired inputs into the struct and
then calls `cycle()`. Outputs keep their values between scans, and so do
`vars`, so you can store state there.

## Reference

| key | meaning |
|---|---|
| `type` | Block type name. Must be a C identifier, e.g. `PID`, `MY_FILTER`. |
| `category` | Group in the library panel. |
| `title`, `description` | Shown in the library tooltip and the properties panel. |
| `color` | Optional header colour, e.g. `"#7a3b8f"`. |
| `inputs`, `outputs` | Ports: `{name, type, default, description, variadic}`. |
| `vars` | Internal state: `{name, type, init}`. |
| `params` | Settings that cannot be wired (see below). |
| `type_params` | Generic types (see below). |
| `includes` | Headers to include, e.g. `["<math.h>", "\"mylib.h\""]`. |
| `globals` | C++ code placed once at file level (helper functions, constants). |
| `members` | Extra C++ struct members, e.g. a driver object: `"plc_dht::Sensor sensor;"` (one per block instance). |
| `init` | C++ code run once at start-up (after parameters are set). |
| `cycle` | C++ code run every scan. |
| `lib_deps` | PlatformIO libraries, e.g. `["paulstoffregen/OneWire@^2.3.8"]`. |
| `targets` | Per-target replacements for `includes/globals/members/init/cycle`, e.g. a `sim:` version that fakes a sensor so the simulator works. |
| `requires` | Board features the block needs, e.g. `[wifi]`. Blocks are greyed out in the palette, and give an error, on boards without them. |
| `requires_blocks` | Block types that must also be in the diagram, e.g. `[MQTT_CLIENT]`. `"A|B"` means one of them (also works in `requires`, e.g. `["bt_classic|ble"]`). |
| `excludes_blocks` | Block types that cannot be in the same program, e.g. `[BT_SERIAL]`. |
| `singleton` | `true`: at most one instance per program (bus or connection settings). |
| `platformio` | Extra `platformio.ini` options, e.g. `{board_build.partitions: huge_app.csv}`. |

### Types

Port types: `BOOL`, `INT` (16 bit), `DINT` (32 bit), `REAL` (float), `TIME`
(int32 milliseconds).
`vars` can also use `SINT`, `USINT`, `UINT` and `UDINT`. Use `UDINT` to store
`millis()` timestamps.

Wires may widen `INT → DINT → REAL` automatically. Any other mix needs a
conversion block (`TO_INT`, `TO_REAL`, …).

Defaults use IEC syntax: `TRUE`/`FALSE`, numbers, and `T#1s500ms` for TIME.

### Variadic inputs

```yaml
inputs:
  - {name: IN, type: BOOL, default: TRUE, variadic: {min: 2, max: 16, default: 2, start: 1}}
```

This creates ports `IN1..INn`. The user picks *n* in the properties panel. In
code, `IN` is an array and `IN_n` holds the count:

```cpp
for (uint8_t i = 0; i < IN_n; i++) r = r && IN[i];
```

### Generic types

```yaml
type_params:
  T: {allowed: ANY_NUM, default: INT}   # groups: ANY, ANY_NUM, ANY_INT, ANY_MAG, or a list
inputs:
  - {name: IN1, type: T}
```

The struct becomes a C++ template, so `T` is a real type in your code. The
editor picks `T` automatically from the first wire you connect, and the user
can also change it.

### Parameters

| kind | example | value in code |
|---|---|---|
| `pin` | `{name: pin, kind: pin, capability: DI}` | the board pin number |
| `enum` | `{name: mode, kind: enum, options: {UP: INPUT_PULLUP, NONE: INPUT}}` | the C expression of the chosen option |
| `int` / `real` | `{name: n, kind: int, min: 1, max: 10, default: 4}` | the number |
| `bool` | `{name: invert, kind: bool}` | `true` / `false` |
| `string` | `{name: topic, kind: string, required: true, default: "temp"}` | a `const char*` string literal |

Pin capabilities come from the board file (`DI`, `DO`, `AI`, `PWM`, `I2C`,
`SPI`). A board can define more capabilities, and your block can then ask for
them. Extra parameter options:

- `default: "@sda"` (pin): use the board's default pin from its `pin_defaults`.
- `shared: true` (pin): several blocks may use the same pin with this
  capability, e.g. an SPI chip select used by a write and a read block.
- `hex: true` (int): shown and entered as hex (`0x76`); decimal works too.
- `secret: true` (string): hidden in the properties panel (passwords). It is
  still stored in plain text in the project file and generated code.
- `required: true` (string): an empty value is an error.

### Communication blocks and shared helpers

The built-in I2C, SPI, WiFi/MQTT and Bluetooth blocks
(`library/blocks/io_comm.yaml`) keep their shared state in helper headers in
`fbdstudio/runtime/` (`plc_i2c.h`, `plc_spi.h`, `plc_mqtt.h`, `plc_text.h`,
and `plc_bt.h` with the transports `plc_bt_classic.h` and `plc_ble.h`). The
sensor and display blocks (`io_sensors.yaml`, `io_displays.yaml`) use
`plc_bme280.h`, `plc_ds18b20.h`, `plc_dht.h`, `plc_lcd.h`, `plc_oled.h` and `plc_tft.h`;
they are good examples of drivers that work step by step across scans
instead of waiting. Every `runtime/*.h` file is copied into the generated
PlatformIO project, so your own blocks can include them too. For example, a
sensor block can read registers through the shared I2C bus:

```yaml
includes: ['"plc_i2c.h"']
requires: [i2c]
cycle: |
  uint8_t b[2];
  OK = plc_i2c::read(0x40, 0xE3, b, 2);
```

MQTT networking runs in its own FreeRTOS task (`plc_mqtt.h`), so a block
never waits for the network. Never print to `Serial` from another task: it
would mix with the monitor protocol.

### Rules of thumb

- Keep `cycle()` fast and **never block**: no `delay()` and no waiting loops.
  Use `millis()` and state variables instead (see `TON` in
  `timers_counters.yaml`).
- Compare elapsed times as `uint32_t` so they still work when `millis()`
  wraps around: `(uint32_t)(millis() - start) >= (uint32_t)PT`.
- Code must be valid C++11 (the ESP32 Arduino core default).
- Arduino functions (`digitalRead`, `analogRead`, `millis`, `Serial`, …) are
  available. The simulator supports pins, time and Serial. For anything else,
  add a `targets: {sim: {...}}` section.

## Boards

Boards are YAML files too (`fbdstudio/library/boards/`, or `boards/` in your
workspace). A board lists the PlatformIO settings and every usable pin with
its capabilities:

```yaml
id: nucleo_f401re
title: STM32 Nucleo-F401RE
platformio: {platform: ststm32, board: nucleo_f401re, framework: arduino}
adc_max: 1023
pins:
  - {id: "PA5", c: "PA5", caps: [DI, DO, PWM], note: "On-board LED LD2"}
  - {id: "PA0", c: "PA0", caps: [DI, DO, AI]}
```

`id` is what the user sees, `c` is the expression used in the generated code.
Set `warn: true` on a pin to show its `note` as a warning when the pin is
used.

Optional board entries:

```yaml
features: [i2c, spi, wifi, bt_classic]      # matched against blocks' requires:
pin_defaults: {sda: GPIO21, scl: GPIO22}    # used by pin params with default "@sda"
                                            # (e.g. tft_cs/tft_dc/... for a built-in display;
                                            #  no pin warning when a block uses its intended pin)
restrictions:                               # pins that stop working when a feature is used
  - feature: wifi
    capability: AI
    pins: [GPIO4, GPIO25]
    message: "is an ADC2 pin: analog inputs on ADC2 do not work while WiFi is used."
```
