// FBD Studio: shared SPI bus for the SPI blocks (uses the Arduino SPI library).
#pragma once
#include <Arduino.h>
#include <SPI.h>
#include "plc_text.h"

namespace plc_spi {

struct State {
  int sck = -1, miso = -1, mosi = -1;
  bool started = false;
};
inline State& st() { static State s; return s; }

// Called by SPI_BUS in setup(); without that block the board's default pins are used.
inline void configure(int sck, int miso, int mosi) {
  State& s = st();
  s.sck = sck; s.miso = miso; s.mosi = mosi;
}

inline void begin() {
  State& s = st();
  if (s.started) return;
  s.started = true;
#if defined(ARDUINO_ARCH_ESP32)
  if (s.sck >= 0) SPI.begin(s.sck, s.miso, s.mosi, -1);
  else SPI.begin();
#else
  SPI.begin();
#endif
}

// One transaction: CS low, exchange n bytes, CS high.
inline void transfer(int cs, uint32_t clock, int bit_order, int mode, const uint8_t* tx, uint8_t* rx, uint8_t n) {
  begin();
  SPI.beginTransaction(SPISettings(clock, bit_order, mode));
  digitalWrite(cs, LOW);
  for (uint8_t i = 0; i < n; i++) rx[i] = SPI.transfer(tx[i]);
  digitalWrite(cs, HIGH);
  SPI.endTransaction();
}

}  // namespace plc_spi
