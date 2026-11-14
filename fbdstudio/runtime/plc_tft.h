// FBD Studio: ST7789 colour TFT (SPI) text display, for the TFT block.
// Uses the Adafruit ST7789 + GFX libraries and the shared SPI bus (plc_spi.h).
//
// Non-blocking: a changed text line is drawn into a small RAM canvas and sent
// in one burst (240 x 18 px = 8.6 KB, ~1.7 ms at 40 MHz); at most one line per
// scan (measured: max. 3.1 ms per scan on an ESP32). Only the start-up (reset +
// init, ~0.6 s with the Adafruit init sequence) happens in one go, on the first scan.
#pragma once
#include <Arduino.h>
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include "plc_spi.h"

namespace plc_tft {

static const int MAX_LINES = 10, LINE_LEN = 40;

struct Tft {
  Adafruit_ST7789* d = nullptr;
  GFXcanvas16* canvas = nullptr;
  int cs = -1, dc = -1, rst = -1, bl = -1;
  int panel_w = 135, panel_h = 240, rotation = 1, size = 2;
  uint16_t fg = 0xFFFF, bg = 0x0000;
  bool started = false, backlight = true;
  int lines = 0, line_h = 18;
  int next = 0;  // round-robin position for sending changed lines
  char want[MAX_LINES][LINE_LEN];
  char shown[MAX_LINES][LINE_LEN];
  bool dirty[MAX_LINES];

  void setup(int cs_, int dc_, int rst_, int bl_, int panel, int rot, int text_size, uint16_t fg_, uint16_t bg_) {
    cs = cs_; dc = dc_; rst = rst_; bl = bl_;
    panel_w = panel / 1000; panel_h = panel % 1000;  // e.g. 135240 -> 135 x 240
    rotation = rot; size = text_size < 1 ? 1 : (text_size > 4 ? 4 : text_size);
    fg = fg_; bg = bg_;
    memset(want, 0, sizeof want);
    memset(shown, 0, sizeof shown);
    for (int i = 0; i < MAX_LINES; i++) dirty[i] = true;
    if (bl >= 0) { pinMode(bl, OUTPUT); digitalWrite(bl, LOW); }  // dark until initialised
  }

  // First scan: init the panel (blocking ~0.6 s, once).
  void start() {
    started = true;
    plc_spi::begin();  // shared SPI bus (SPI_BUS pins or the board defaults)
    d = new Adafruit_ST7789(&SPI, cs, dc, rst);
    d->init(panel_w, panel_h);
    d->setSPISpeed(40000000);
    d->setRotation(rotation);
    d->fillScreen(bg);
    line_h = 8 * size + 2;
    lines = d->height() / line_h;
    if (lines > MAX_LINES) lines = MAX_LINES;
    canvas = new GFXcanvas16(d->width(), line_h);
    canvas->cp437(true);
    canvas->setTextWrap(false);
    canvas->setTextSize(size);
    if (bl >= 0) digitalWrite(bl, backlight ? HIGH : LOW);
  }

  // UTF-8 "°" -> code page 437 degree sign; other non-ASCII -> '?'
  static void to_cp437(const char* in, char* out, size_t n) {
    size_t o = 0;
    for (const unsigned char* p = (const unsigned char*)in; *p && o + 1 < n; p++) {
      if (p[0] == 0xC2 && p[1] == 0xB0) { out[o++] = (char)0xF8; p++; }
      else if (*p >= 0x80) { out[o++] = '?'; while ((p[1] & 0xC0) == 0x80) p++; }
      else out[o++] = (char)*p;
    }
    out[o] = 0;
  }

  void set_line(int i, const char* text) {
    if (i < 0 || i >= MAX_LINES) return;
    char conv[LINE_LEN];
    to_cp437(text, conv, sizeof conv);
    if (strcmp(conv, want[i]) != 0) { strcpy(want[i], conv); dirty[i] = true; }
  }

  void set_backlight(bool on) {
    if (on == backlight) return;
    backlight = on;
    if (bl >= 0 && started) digitalWrite(bl, on ? HIGH : LOW);
  }

  // Sends at most one changed line.
  void update() {
    if (!d) return;
    for (int k = 0; k < lines; k++) {
      int i = (next + k) % lines;
      if (!dirty[i] || strcmp(want[i], shown[i]) == 0) { dirty[i] = false; continue; }
      canvas->fillScreen(bg);
      canvas->setTextColor(fg);
      canvas->setCursor(2, 1);
      canvas->print(want[i]);
      d->drawRGBBitmap(0, i * line_h, canvas->getBuffer(), canvas->width(), canvas->height());
      strcpy(shown[i], want[i]);
      dirty[i] = false;
      next = (i + 1) % lines;
      return;
    }
  }
};

}  // namespace plc_tft
