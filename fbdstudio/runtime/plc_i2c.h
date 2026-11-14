// FBD Studio: shared I2C bus for the I2C blocks (uses the Arduino Wire library).
#pragma once
#include <Arduino.h>
#include <Wire.h>
#include "plc_text.h"

namespace plc_i2c {

struct State {
  int sda = -1, scl = -1;
  uint32_t freq = 100000;
  bool started = false, ok = false;
};
inline State& st() { static State s; return s; }

// Called by I2C_BUS in setup(); without that block the board's default pins are used.
inline void configure(int sda, int scl, uint32_t freq) {
  State& s = st();
  s.sda = sda; s.scl = scl; s.freq = freq;
}

// Starts the bus on first use (after all blocks ran init()).
inline bool begin() {
  State& s = st();
  if (!s.started) {
    s.started = true;
#if defined(ARDUINO_ARCH_ESP32)
    s.ok = s.sda >= 0 ? Wire.begin(s.sda, s.scl, s.freq) : Wire.begin();
    Wire.setTimeOut(20);  // ms: a stuck bus must not stall the scan cycle for long
#else
    Wire.begin();
    Wire.setClock(s.freq);
    s.ok = true;
#endif
  }
  return s.ok;
}

inline bool probe(uint8_t addr) {
  if (!begin()) return false;
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

// reg < 0: plain read without a register address.
inline bool read(uint8_t addr, int reg, uint8_t* buf, uint8_t n) {
  if (!begin()) return false;
  if (reg >= 0) {
    Wire.beginTransmission(addr);
    Wire.write((uint8_t)reg);
    if (Wire.endTransmission(false) != 0) return false;  // repeated start
  }
  if (Wire.requestFrom((uint16_t)addr, (size_t)n, true) != n) return false;
  for (uint8_t i = 0; i < n; i++) buf[i] = (uint8_t)Wire.read();
  return true;
}

inline bool write(uint8_t addr, int reg, const uint8_t* buf, uint8_t n) {
  if (!begin()) return false;
  Wire.beginTransmission(addr);
  if (reg >= 0) Wire.write((uint8_t)reg);
  for (uint8_t i = 0; i < n; i++) Wire.write(buf[i]);
  return Wire.endTransmission() == 0;
}

}  // namespace plc_i2c
