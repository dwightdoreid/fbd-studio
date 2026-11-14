// FBD Studio: classic Bluetooth serial (SPP) transport for the BT blocks. Original ESP32 only.
// Works with Android "serial Bluetooth terminal" apps and PCs (a COM port).
// iPhones do not support classic Bluetooth serial: use BLE_UART (plc_ble.h) for them.
#pragma once
#include <Arduino.h>
#include <BluetoothSerial.h>
#include "plc_bt.h"

namespace plc_bt_classic {

inline BluetoothSerial& bt() { static BluetoothSerial b; return b; }

inline bool connected() { return bt().hasClient(); }
inline int read() { return bt().available() ? bt().read() : -1; }
inline void write(const char* data, size_t len) { bt().write((const uint8_t*)data, len); }

inline bool start(const char* name) {
  static const plc_bt::Transport t = {connected, read, write};
  plc_bt::set_transport(&t);
  return bt().begin(name);
}

}  // namespace plc_bt_classic
