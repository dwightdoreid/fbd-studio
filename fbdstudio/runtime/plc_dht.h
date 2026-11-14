// FBD Studio: DHT11 / DHT22 (AM2302) temperature + humidity sensors, for the DHT block.
//
// The start signal (DHT11: 18 ms LOW, DHT22: 1 ms LOW) is done across scans, so
// the scan never waits for it. The 40 data bits are then read in one go, which
// takes about 4-5 ms with interrupts off (the timing is too fine for anything
// else). Sensors may only be read every 1 s (DHT11) / 2 s (DHT22).
// Needs a pull-up (4.7-10 kOhm) on the data line; most DHT modules have one.
#pragma once
#include <Arduino.h>

namespace plc_dht {

struct Sensor {
  int pin = -1;
  bool dht22 = true;
  enum State : uint8_t { IDLE, START_LOW } state = IDLE;
  uint32_t t0 = 0, last = 0;
  bool first = true;
  uint8_t fails = 0;

  void setup(int p, bool is_dht22) {
    pin = p;
    dht22 = is_dht22;
    pinMode(pin, INPUT_PULLUP);
  }

  // Time the line stays at 'level', in µs; UINT32_MAX on timeout.
  uint32_t pulse(int level, uint32_t timeout_us) {
    uint32_t start = micros();
    while (digitalRead(pin) == level)
      if (micros() - start > timeout_us) return UINT32_MAX;
    return micros() - start;
  }

  bool read_bits(uint8_t d[5]) {
    uint32_t lows[40], highs[40];
    bool ok = true;
    pinMode(pin, INPUT_PULLUP);  // release the line: the sensor answers (pinMode may lock: not inside noInterrupts)
    noInterrupts();
    delayMicroseconds(55);
    if (pulse(LOW, 120) == UINT32_MAX || pulse(HIGH, 120) == UINT32_MAX) ok = false;  // 80 µs low + 80 µs high
    for (int i = 0; ok && i < 40; i++) {
      lows[i] = pulse(LOW, 120);    // ~50 µs
      highs[i] = pulse(HIGH, 120);  // ~27 µs = 0, ~70 µs = 1
      if (lows[i] == UINT32_MAX || highs[i] == UINT32_MAX) ok = false;
    }
    interrupts();
    if (!ok) return false;
    for (int i = 0; i < 5; i++) d[i] = 0;
    for (int i = 0; i < 40; i++)
      if (highs[i] > lows[i]) d[i / 8] |= (uint8_t)(0x80 >> (i % 8));
    return (uint8_t)(d[0] + d[1] + d[2] + d[3]) == d[4];
  }

  // Call every scan. Returns true when T (°C) and H (%) were updated.
  bool step(uint32_t interval_ms, float& T, float& H, bool& ok) {
    uint32_t now = millis();
    uint32_t min_gap = dht22 ? 2000 : 1000;
    if (interval_ms < min_gap) interval_ms = min_gap;
    if (state == IDLE) {
      // after power-up the sensor needs ~1 s before the first reading
      if (first ? now < 1500 : now - last < interval_ms) return false;
      first = false;
      last = now;
      pinMode(pin, OUTPUT);
      digitalWrite(pin, LOW);  // start signal
      t0 = now;
      state = START_LOW;
      return false;
    }
    // START_LOW
    if (now - t0 < (dht22 ? 2u : 20u)) return false;
    state = IDLE;
    uint8_t d[5];
    if (!read_bits(d)) {
      if (++fails >= 3) ok = false;  // tolerate single bad readings
      return false;
    }
    fails = 0;
    if (dht22) {
      H = ((d[0] << 8) | d[1]) * 0.1f;
      T = (((d[2] & 0x7F) << 8) | d[3]) * 0.1f;
      if (d[2] & 0x80) T = -T;
    } else {
      H = d[0] + d[1] * 0.1f;
      T = d[2] + (d[3] & 0x7F) * 0.1f;
      if (d[3] & 0x80) T = -T;
    }
    ok = true;
    return true;
  }
};

}  // namespace plc_dht
