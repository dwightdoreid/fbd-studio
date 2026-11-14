// FBD Studio: DS18B20 / DS18S20 / DS1822 1-Wire temperature sensors, for the DS18B20 block.
//
// Non-blocking: a conversion takes up to 750 ms, so the block starts it, comes
// back when it is done and then reads the result (a few ms of 1-Wire traffic
// per step). Several sensors can share one pin; 'index' selects one of them in
// the order the 1-Wire search finds them (their ROM codes are printed once).
// Needs a 4.7 kOhm pull-up from the data pin to 3.3 V. Parasite power is not supported.
#pragma once
#include <Arduino.h>
#include <OneWire.h>

namespace plc_ds18b20 {

struct Sensor {
  OneWire* ow = nullptr;
  int pin = -1;
  uint8_t index = 0;
  uint8_t bits = 12;  // resolution 9..12
  uint8_t rom[8];
  enum State : uint8_t { SEARCH, IDLE, CONVERTING } state = SEARCH;
  uint32_t t0 = 0;
  bool searched_once = false, listed = false;
  uint8_t fails = 0;

  void setup(int p, int idx, int resolution) {
    pin = p;
    index = (uint8_t)idx;
    bits = (uint8_t)(resolution < 9 ? 9 : (resolution > 12 ? 12 : resolution));
    ow = new OneWire((uint8_t)p);
  }

  uint32_t conv_ms() const { return 750u >> (12 - bits); }  // 94, 188, 375, 750 ms

  static bool family_ok(uint8_t f) { return f == 0x28 || f == 0x10 || f == 0x22; }

  // Finds the sensor; prints all sensors on the pin the first time.
  bool search() {
    uint8_t a[8];
    int n = 0;
    bool found = false;
    ow->reset_search();
    while (ow->search(a)) {
      if (OneWire::crc8(a, 7) != a[7] || !family_ok(a[0])) continue;
      if (!listed) {
        char line[64];
        snprintf(line, sizeof line, "DS18B20 on pin %d: index %d = %02X-%02X%02X%02X%02X%02X%02X",
                 pin, n, a[0], a[6], a[5], a[4], a[3], a[2], a[1]);
        Serial.println(line);
      }
      if (n == index) { memcpy(rom, a, 8); found = true; }
      n++;
    }
    if (!listed && n == 0) {
      char line[80];
      snprintf(line, sizeof line, "DS18B20 on pin %d: no sensor found (check wiring and the 4.7k pull-up)", pin);
      Serial.println(line);
    }
    listed = true;
    if (!found) return false;
    if (rom[0] != 0x10) {  // set the resolution (DS18S20 has a fixed 9 bits)
      ow->reset();
      ow->select(rom);
      ow->write(0x4E);  // write scratchpad: TH, TL, config
      ow->write(0x4B);
      ow->write(0x46);
      ow->write((uint8_t)(((bits - 9) << 5) | 0x1F));
    }
    return true;
  }

  // Call every scan. Returns true when a new temperature was read into T.
  bool step(uint32_t interval_ms, float& T, bool& ok) {
    uint32_t now = millis();
    switch (state) {
      case SEARCH:
        if (searched_once && now - t0 < 5000) return false;  // retry every 5 s
        searched_once = true;
        t0 = now;
        if (!search()) { ok = false; return false; }
        fails = 0;
        state = IDLE;
        t0 = now - interval_ms;  // measure right away
        return false;
      case IDLE:
        if (now - t0 < interval_ms) return false;
        if (!ow->reset()) { fail(ok); return false; }  // no presence pulse
        ow->select(rom);
        ow->write(0x44);  // start conversion
        t0 = now;
        state = CONVERTING;
        return false;
      case CONVERTING: {
        uint32_t wait = rom[0] == 0x10 ? 750 : conv_ms();
        if (now - t0 < wait + 10) return false;
        uint8_t d[9];
        ow->reset();
        ow->select(rom);
        ow->write(0xBE);  // read scratchpad
        for (int i = 0; i < 9; i++) d[i] = ow->read();
        state = IDLE;  // t0 stays at the conversion start: one measurement per interval
        if (OneWire::crc8(d, 8) != d[8] || (d[0] == 0xFF && d[1] == 0xFF)) { fail(ok); return false; }
        int16_t raw = (int16_t)((d[1] << 8) | d[0]);
        if (rom[0] == 0x10) {  // DS18S20: 9 bit + "count remain" for more resolution
          raw = (int16_t)(raw << 3);
          if (d[7] == 0x10) raw = (int16_t)((raw & 0xFFF0) + 12 - d[6]);
        } else if (bits < 12) {
          raw &= (int16_t)~((1 << (12 - bits)) - 1);  // undefined low bits
        }
        T = raw / 16.0f;
        fails = 0;
        ok = true;
        return true;
      }
    }
    return false;
  }

  void fail(bool& ok) {
    state = IDLE;
    if (++fails >= 3) {  // sensor gone: search again
      ok = false;
      state = SEARCH;
      t0 = millis();
    }
  }
};

}  // namespace plc_ds18b20
