"""Communication blocks: board features, singletons, required blocks, pin rules, helpers."""
import subprocess

import pytest

from fbdstudio import builder
from fbdstudio.compiler import compile_project
from fbdstudio.project import Project

from test_compiler import blk, errors


def compile_(library, board, blocks, wires=(), target="sim"):
    return compile_project(Project(name="t", blocks=list(blocks), wires=list(wires)), library, board, target=target)


def test_required_blocks_and_singletons(library, board):
    res = compile_(library, board, [blk("p", "MQTT_PUB")])
    assert any("needs a MQTT_CLIENT block" in m for m in errors(res))
    res = compile_(library, board, [blk("c", "MQTT_CLIENT")])
    assert any("needs a WIFI block" in m for m in errors(res))
    res = compile_(library, board, [blk("w", "WIFI")])
    assert errors(res) == ["WIFI_w.ssid: enter a value"]
    res = compile_(library, board, [blk("w1", "WIFI"), blk("w2", "WIFI", x=50)])
    assert sum("only one WIFI" in m for m in errors(res)) == 2
    wifi = blk("w", "WIFI", params={"ssid": "net"})
    res = compile_(library, board, [wifi, blk("c", "MQTT_CLIENT"), blk("p", "MQTT_PUB"), blk("s", "MQTT_SUB")])
    assert res.ok, errors(res)


def test_board_features(library, board):
    no_radio = board.model_copy(update={"features": ["i2c", "spi"], "title": "Tiny board"})
    res = compile_(library, no_radio, [blk("w", "WIFI"), blk("i", "I2C_READ")])
    msgs = errors(res)
    assert any("WIFI needs wifi, which Tiny board does not have" in m for m in msgs)
    assert not any("I2C_READ" in m for m in msgs)


def test_adc2_pins_blocked_while_wifi_is_used(library, board):
    ai = blk("a", "AI", params={"pin": "GPIO4"})  # ADC2
    assert compile_(library, board, [ai]).ok
    res = compile_(library, board, [ai, blk("w", "WIFI", params={"ssid": "n"})])
    assert any("GPIO4 is an ADC2 pin" in m for m in errors(res))
    ok = blk("a", "AI", params={"pin": "GPIO34"})  # ADC1
    assert compile_(library, board, [ok, blk("w", "WIFI", params={"ssid": "n"})]).ok


def test_bus_default_pins_and_conflicts(library, board):
    res = compile_(library, board, [blk("bus", "I2C_BUS")], target="device")
    assert res.ok, errors(res)
    assert "bus.sda = 21;" in res.code and "bus.scl = 22;" in res.code
    # a DO on the SDA pin conflicts
    res = compile_(library, board, [blk("bus", "I2C_BUS"), blk("d", "DO", params={"pin": "GPIO21"})])
    assert any("Pin GPIO21 is used by several blocks" in m for m in errors(res))
    # two SPI transfers may share a chip select, a DO may not
    a = blk("a", "SPI_TRANSFER", params={"cs": "GPIO5"})
    b = blk("b", "SPI_TRANSFER", x=40, params={"cs": "GPIO5"})
    assert compile_(library, board, [a, b]).ok
    res = compile_(library, board, [a, b, blk("d", "DO", params={"pin": "GPIO5"})])
    assert any("Pin GPIO5" in m for m in errors(res))


def test_string_and_hex_params(library, board, tmp_path):
    w = blk("w", "WIFI", params={"ssid": 'My "Net" ?? ü', "password": "p\\w"})
    r = blk("r", "I2C_READ", params={"addr": "0x76", "reg": "0xFA"})
    res = compile_(library, board, [w, r], target="device")
    assert res.ok, errors(res)
    assert r'w.ssid = "My \"Net\" \?\? \303\274";' in res.code
    assert r'w.password = "p\\w";' in res.code
    assert "r.addr = 118;" in res.code and "r.reg = 250;" in res.code
    res = compile_(library, board, [blk("r", "I2C_READ", params={"addr": "0x80"})])
    assert any("outside 1.0..127.0" in m for m in errors(res))
    # the simulator version compiles too (comm blocks are no-ops there)
    sim = compile_(library, board, [w, r])
    builder.build_sim(sim, tmp_path)


def test_bluetooth_sets_partition_table(library, board):
    res = compile_(library, board, [blk("bt", "BT_SERIAL"), blk("s", "BT_SEND")], target="device")
    assert res.ok, errors(res)
    assert '#include "plc_bt_classic.h"' in res.code
    assert res.platformio == {"board_build.partitions": "huge_app.csv"}
    assert "board_build.partitions = huge_app.csv" in builder.platformio_ini(board, res, 115200)
    files = builder.project_files(board, res, 115200)
    assert {"include/plcrt.h", "include/plc_bt.h", "include/plc_bt_classic.h", "include/plc_ble.h", "include/plc_text.h"} <= set(files)


HELPER_TEST = r'''
#include <assert.h>
#include <string.h>
#include "plc_text.h"
using namespace plc_text;
int main() {
  char b[32];
  format(true, b, sizeof b, 0, 1); assert(!strcmp(b, "ON"));
  format(false, b, sizeof b, 0, 0); assert(!strcmp(b, "0"));
  format(21.456f, b, sizeof b, 2, 0); assert(!strcmp(b, "21.46"));
  format((int16_t)-5, b, sizeof b, 0, 0); assert(!strcmp(b, "-5"));
  bool x; assert(parse(" On ", x) && x); assert(parse("false", x) && !x); assert(parse("2", x) && x);
  assert(!parse("maybe", x));
  int16_t i; assert(parse("40000", i) && i == 32767); assert(parse("-1.6", i) && i == -2); assert(parse("true", i) && i == 1);
  int32_t d; assert(parse("123456", d) && d == 123456); assert(!parse("12abc", d));
  float f; assert(parse("2.5", f) && f == 2.5f); assert(!parse("", f));
  uint8_t by[4] = {0xFF, 0x38};
  assert(unpack(by, 2, true, false) == 0xFF38);
  assert(unpack(by, 2, true, true) == -200);
  assert(unpack(by, 2, false, false) == 0x38FF);
  pack(0x1234, by, 2, true); assert(by[0] == 0x12 && by[1] == 0x34);
  pack(0x1234, by, 2, false); assert(by[0] == 0x34 && by[1] == 0x12);
  pack(-2, by, 4, true); assert(unpack(by, 4, true, true) == -2);
  return 0;
}
'''


def test_text_and_byte_helpers(tmp_path):
    cxx = builder.find_cxx()
    if cxx is None:
        pytest.skip("no C++ compiler")
    src = tmp_path / "t.cpp"
    src.write_text(HELPER_TEST)
    exe = tmp_path / "t"
    subprocess.run([cxx, "-std=c++11", f"-I{builder.RUNTIME_DIR}", str(src), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)


def test_ble_or_classic_transport(library, board):
    send = blk("s", "BT_SEND")
    res = compile_(library, board, [send])
    assert any("needs a BT_SERIAL or BLE_UART block" in m for m in errors(res))
    res = compile_(library, board, [blk("ble", "BLE_UART", params={"passkey": 123456}), send, blk("r", "BT_RECV")], target="device")
    assert res.ok, errors(res)
    assert '#include "plc_ble.h"' in res.code and "plc_bt_classic.h" not in res.code
    assert "ble.passkey = 123456;" in res.code
    res = compile_(library, board, [blk("ble", "BLE_UART"), blk("bt", "BT_SERIAL")])
    assert sum("cannot be used in the same program" in m for m in errors(res)) == 2
    # a board with BLE only (like ESP32-S3/C3): BLE blocks work, classic does not
    ble_only = board.model_copy(update={"features": ["wifi", "ble"], "title": "S3 board"})
    assert compile_(library, ble_only, [blk("ble", "BLE_UART"), send]).ok
    res = compile_(library, ble_only, [blk("bt", "BT_SERIAL"), send])
    assert any("BT_SERIAL needs bt_classic" in m for m in errors(res))
    none = board.model_copy(update={"features": [], "title": "Plain board"})
    res = compile_(library, none, [send])
    assert any("BT_SEND needs bt_classic or ble" in m for m in errors(res))
