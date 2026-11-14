// FBD Studio: character LCD (HD44780) with a PCF8574 I2C backpack, for the LCD_I2C block.
//
// Non-blocking: the block renders the wanted text into 'want'; update() sends
// only characters that differ from what is on the screen ('shown'), at most
// 'budget' characters per call, so a scan never spends long on the display.
// Backpack wiring (almost all modules): P0=RS P1=RW P2=EN P3=backlight P4..P7=D4..D7.
#pragma once
#include <Arduino.h>
#include "plc_i2c.h"

namespace plc_lcd {

static const int MAX_COLS = 20, MAX_ROWS = 4;

struct Lcd {
  uint8_t addr = 0x27, cols = 16, rows = 2;
  bool ready = false, backlight = true;
  uint32_t last_try = 0;
  bool tried = false;
  int cur_r = -1, cur_c = -1;  // where the controller's cursor is (-1 = unknown)
  char want[MAX_ROWS][MAX_COLS];
  char shown[MAX_ROWS][MAX_COLS];

  Lcd() {
    memset(want, ' ', sizeof want);
    memset(shown, ' ', sizeof shown);
  }

  bool raw(uint8_t v) {
    Wire.beginTransmission(addr);
    Wire.write(v);
    return Wire.endTransmission() == 0;
  }
  // one 4-bit transfer: data on D4..D7, pulse EN
  bool nibble(uint8_t hi4, bool rs) {
    uint8_t v = (hi4 & 0xF0) | (rs ? 0x01 : 0) | (backlight ? 0x08 : 0);
    Wire.beginTransmission(addr);
    Wire.write(v | 0x04);  // EN high
    Wire.write(v);         // EN low: the controller latches the nibble
    return Wire.endTransmission() == 0;
  }
  bool send(uint8_t b, bool rs) {
    return nibble(b & 0xF0, rs) && nibble((uint8_t)(b << 4), rs);
  }

  // Blocking start-up sequence (~10 ms), only on the first scan or after an error.
  bool init() {
    if (!plc_i2c::begin()) return false;
    cur_r = cur_c = -1;
    if (!raw(backlight ? 0x08 : 0)) return false;
    delay(2);
    for (int i = 0; i < 3; i++) { nibble(0x30, false); delayMicroseconds(4500); }  // force 8-bit mode
    nibble(0x20, false);  // switch to 4-bit mode
    delayMicroseconds(150);
    bool ok = send(0x28, false)    // 4-bit, 2 lines, 5x8 font
              && send(0x0C, false)  // display on, cursor off
              && send(0x06, false)  // entry mode: increment
              && send(0x01, false); // clear
    delay(2);
    for (int r = 0; r < MAX_ROWS; r++)
      for (int c = 0; c < MAX_COLS; c++) shown[r][c] = ' ';
    return ok;
  }

  void set_line(int r, const char* text) {
    if (r < 0 || r >= rows) return;
    int c = 0;
    for (const unsigned char* p = (const unsigned char*)text; *p && c < cols; p++) {
      unsigned char ch = *p;
      if (ch == 0xC2 && p[1] == 0xB0) { ch = 0xDF; p++; }  // UTF-8 degree sign -> LCD ROM degree sign
      else if (ch >= 0x80) ch = '?';
      want[r][c++] = (char)ch;
    }
    while (c < cols) want[r][c++] = ' ';
  }

  // Sends up to 'budget' changed characters. Returns false on a bus error.
  bool update(int budget) {
    if (!ready) {
      if (tried && millis() - last_try < 3000) return false;
      tried = true;
      last_try = millis();
      ready = init();
      if (!ready) return false;
    }
    static const uint8_t row_addr[4] = {0x00, 0x40, 0x14, 0x54};
    for (int r = 0; r < rows && budget > 0; r++) {
      for (int c = 0; c < cols && budget > 0; c++) {
        if (want[r][c] == shown[r][c]) continue;
        if (cur_r != r || cur_c != c) {
          if (!send(0x80 | (row_addr[r] + c), false)) { ready = false; return false; }
        }
        if (!send((uint8_t)want[r][c], true)) { ready = false; return false; }
        shown[r][c] = want[r][c];
        cur_r = r;
        cur_c = c + 1;
        budget--;
      }
    }
    return true;
  }

  bool set_backlight(bool on) {
    if (on == backlight) return true;
    backlight = on;
    return !ready || raw(on ? 0x08 : 0);
  }
};

}  // namespace plc_lcd
