"""Sensor and display blocks: driver maths on the PC, simulator output, checks."""
import struct
import subprocess

import pytest

from fbdstudio import builder
from fbdstudio.compiler import compile_project
from fbdstudio.project import Project

from test_compiler import blk, errors

# Worked example from the Bosch BMP280 datasheet (section 3.12):
# adc_T = 519888 -> T = 25.08 °C (t_fine = 128422); adc_P = 415148 -> p = 100653.27 Pa
CALIB = dict(T1=27504, T2=26435, T3=-1000, P1=36477, P2=-10685, P3=3024, P4=2855, P5=140, P6=-7,
             P7=15500, P8=-14600, P9=6000)


def calib_bytes():
    c = CALIB
    b = struct.pack("<HhhHhhhhhhhh", c["T1"], c["T2"], c["T3"], c["P1"], c["P2"], c["P3"], c["P4"],
                    c["P5"], c["P6"], c["P7"], c["P8"], c["P9"])
    return b + b"\x00\x4b"  # 0xA0 (unused), 0xA1 = H1


def raw_bytes(adc_p, adc_t):
    return bytes([adc_p >> 12 & 0xFF, adc_p >> 4 & 0xFF, (adc_p & 0xF) << 4,
                  adc_t >> 12 & 0xFF, adc_t >> 4 & 0xFF, (adc_t & 0xF) << 4, 0, 0])


def c_array(b):
    return "{" + ",".join(str(x) for x in b) + "}"


DRIVER_TEST = r'''
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "plc_bme280.h"
#include "plc_text.h"
int main() {
  // ---- BME280/BMP280 compensation: datasheet example
  const uint8_t c88[26] = CALIB;
  const uint8_t raw[8] = RAW;
  plc_bme280::Calib c;
  plc_bme280::parse_calib(c88, nullptr, c);
  assert(c.T1 == 27504 && c.T3 == -1000 && c.P9 == 6000 && c.H1 == 0x4b);
  int32_t ap, at, ah, tf;
  plc_bme280::parse_raw(raw, ap, at, ah);
  assert(at == 519888 && ap == 415148);
  double T = plc_bme280::temperature(c, at, tf);
  double P = plc_bme280::pressure(c, ap, tf);
  printf("T=%.4f t_fine=%d P=%.4f\n", T, tf, P);
  assert(fabs(T - 25.08) < 0.005 && tf == 128422);
  assert(fabs(P - 100653.27) < 0.05);
  // humidity calibration bytes: 12-bit signed H4/H5 packing
  const uint8_t e1[7] = {0x6A, 0x01, 0x00, 0x13, 0x25, 0x03, 0x1E};
  plc_bme280::parse_calib(c88, e1, c);
  assert(c.H2 == 362 && c.H3 == 0 && c.H4 == 0x135 && c.H5 == 0x32 && c.H6 == 30);
  const uint8_t e1neg[7] = {0, 0, 0, 0xFF, 0xFF, 0xFF, 0};
  plc_bme280::parse_calib(c88, e1neg, c);
  assert(c.H4 == -1 && c.H5 == -1);

  // ---- display templates
  char out[48];
  float v[4] = {21.456f, -3.0f, 1013.25f, 7.0f};
  plc_text::render("Temp {1:1} C", v, 4, 2, out, sizeof out); assert(!strcmp(out, "Temp 21.5 C"));
  plc_text::render("{2} / {3:0} hPa", v, 4, 2, out, sizeof out); assert(!strcmp(out, "-3.00 / 1013 hPa"));
  plc_text::render("{{1}} {9} {x} {4:0}%", v, 4, 1, out, sizeof out); assert(!strcmp(out, "{1}} {9} {x} 7%"));
  plc_text::render("abcdefghij{1}", v, 4, 1, out, 12); assert(!strcmp(out, "abcdefghij2"));
  return 0;
}
'''


def test_driver_maths(tmp_path):
    cxx = builder.find_cxx()
    if cxx is None:
        pytest.skip("no C++ compiler")
    src = DRIVER_TEST.replace("CALIB", c_array(calib_bytes())).replace("RAW", c_array(raw_bytes(415148, 519888)))
    (tmp_path / "t.cpp").write_text(src)
    exe = tmp_path / "t"
    subprocess.run([cxx, "-std=c++11", f"-I{builder.RUNTIME_DIR}", str(tmp_path / "t.cpp"), "-o", str(exe)], check=True)
    out = subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout
    assert "T=25.08" in out


def compile_(library, board, blocks, target="sim"):
    return compile_project(Project(name="t", blocks=list(blocks)), library, board, target=target)


def test_displays_print_in_simulator(library, board, tmp_path):
    lcd = blk("l", "LCD_I2C", values={"V1": "21.456"}, params={"line1": "Temp {1:1} °C", "line2": "V2={2:0}"})
    oled = blk("o", "OLED", x=50, values={"V1": "5"}, params={"font": "large (16 x 4)", "line1": "Hello", "line3": "{1:2}"})
    tft = blk("t", "TFT", x=90, values={"V2": "3"}, params={"cs": "GPIO15", "dc": "GPIO2", "rst": "GPIO4", "bl": "GPIO32",
                                                       "text_size": "3 (18 x 24 px)", "line1": "A{2:0}", "line2": "B"})
    res = compile_(library, board, [lcd, oled, tft])
    assert res.ok, errors(res)
    exe = builder.build_sim(res, tmp_path)
    proc = subprocess.Popen([str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        lines = []
        while len(lines) < 30:
            line = proc.stdout.readline().decode("utf-8").strip()
            if line.startswith(("LCD:", "OLED:", "TFT:")):
                lines.append(line)
            if all(any(l.startswith(k) for l in lines) for k in ("LCD:", "OLED:", "TFT:")):
                break
    finally:
        proc.kill()
    assert "LCD: Temp 21.5 °C | V2=0" in lines
    assert "OLED: Hello |  | 5.00 |" in lines  # (4th line empty; trailing space stripped)
    # 135x240 landscape, size 3: 5 lines of 13 characters
    assert "TFT: A3 | B |  |  |" in lines


def test_sensor_blocks(library, board):
    # sensors compile for the device with their drivers; several DS18B20 may share a pin
    blocks = [blk("b", "BME280"), blk("d1", "DS18B20", params={"pin": "GPIO4", "index": 0}),
              blk("d2", "DS18B20", x=10, params={"pin": "GPIO4", "index": 1}),
              blk("h", "DHT", params={"pin": "GPIO16"})]
    res = compile_(library, board, blocks, target="device")
    assert res.ok, errors(res)
    assert "plc_bme280::Sensor sensor;" in res.code and "OneWire" in "".join(res.lib_deps)
    # a DHT may not share its pin
    res = compile_(library, board, [blk("h", "DHT", params={"pin": "GPIO4"}), blk("d", "DS18B20", params={"pin": "GPIO4"})])
    assert any("Pin GPIO4" in m for m in errors(res))
    # the simulator variants have no driver objects
    res = compile_(library, board, blocks)
    assert res.ok and "plc_bme280" not in res.code


def test_tft_on_ideaspark_board(library):
    from fbdstudio.boards import Boards
    board = Boards().get("ideaspark_esp32_lcd114")
    tft = blk("t", "TFT", values={"V1": "21.5"}, params={"line1": "Temp {1:1} °C"})
    res = compile_(library, board, [tft], target="device")
    assert res.ok, errors(res)
    # built-in display pins filled in, and no "pin used by the LCD" warnings for the TFT block itself
    for line in ("t.cs = 15;", "t.dc = 2;", "t.rst = 4;", "t.bl = 32;", "t.panel = 135240;"):
        assert line in res.code
    assert not [d for d in res.diagnostics if d.level == "warning"]
    # another block on a display pin conflicts with it
    res = compile_(library, board, [tft, blk("d", "DO", params={"pin": "GPIO2"})])
    assert any("Pin GPIO2" in m for m in errors(res))
    # ... and without the TFT block it is only a warning
    res = compile_(library, board, [blk("d", "DO", params={"pin": "GPIO2"})])
    assert res.ok and any("Built-in LCD: DC" in d.message for d in res.diagnostics)
    # on a board without a built-in display the pins must be chosen
    from fbdstudio.boards import Boards as B
    res = compile_(library, B().get("esp32dev"), [blk("t", "TFT")])
    assert any("t.cs: select a pin" in m.replace("TFT_t", "t") for m in errors(res))
