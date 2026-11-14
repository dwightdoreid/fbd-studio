// FBD Studio: I2C OLED (SSD1306 / SH1106) text display for the OLED block, using U8g2.
//
// Non-blocking: text is drawn into U8g2's RAM buffer only when it changed, and
// the buffer is sent to the display in pieces of 8 tiles (64 bytes, ~1.5 ms at
// 400 kHz) per scan, instead of the whole 1 KB frame (~25 ms) at once.
#pragma once
#include <Arduino.h>
#include <U8g2lib.h>
#include "plc_i2c.h"

namespace plc_oled {

static const int MAX_LINES = 6, LINE_LEN = 32;

struct Oled {
  U8G2* d = nullptr;
  bool started = false, ok = false;
  uint8_t lines = 6, line_h = 10, ascent = 8;
  int chunk = -1;  // next chunk to send, -1 = nothing pending
  int chunks = 0;  // chunks per frame
  int tiles_w = 16, tiles_h = 8;
  char text[MAX_LINES][LINE_LEN];

  // controller: 0 = SSD1306 128x64, 1 = SH1106 128x64, 2 = SSD1306 128x32
  // font: 0 = small (6x10, 21 columns), 1 = large (8x13, 16 columns)
  void setup(int controller, int i2c_addr, int font) {
    if (controller == 1) d = new U8G2_SH1106_128X64_NONAME_F_HW_I2C(U8G2_R0, U8X8_PIN_NONE);
    else if (controller == 2) d = new U8G2_SSD1306_128X32_UNIVISION_F_HW_I2C(U8G2_R0, U8X8_PIN_NONE);
    else d = new U8G2_SSD1306_128X64_NONAME_F_HW_I2C(U8G2_R0, U8X8_PIN_NONE);
    addr = (uint8_t)i2c_addr;
    d->setI2CAddress((uint8_t)(i2c_addr * 2));
    int height = controller == 2 ? 32 : 64;
    if (font == 1) { line_h = 16; ascent = 12; d->setFont(u8g2_font_8x13B_tf); }
    else { line_h = 10; ascent = 8; d->setFont(u8g2_font_6x10_tf); }
    lines = (uint8_t)(height / line_h);
    if (lines > MAX_LINES) lines = MAX_LINES;
    memset(text, 0, sizeof text);
  }

  uint8_t addr = 0x3C;
  uint32_t last_try = 0;

  // First scan (and retries): start the bus and the display (sends the init sequence once).
  bool start() {
    started = true;
    last_try = millis();
    if (!d || !plc_i2c::begin() || !plc_i2c::probe(addr)) return false;  // no display at this address
    d->setBusClock(plc_i2c::st().freq);  // keep the clock the other I2C blocks use
    ok = d->begin();
    text[0][0] = 1;  // force the first frame to be drawn
    tiles_w = d->getBufferTileWidth();
    tiles_h = d->getBufferTileHeight();
    chunks = tiles_h * ((tiles_w + 7) / 8);
    return ok;
  }

  // Draws the lines into the buffer if any of them changed.
  void set_lines(char (*want)[LINE_LEN], int n) {
    bool changed = false;
    for (int i = 0; i < n && i < lines; i++)
      if (strncmp(text[i], want[i], LINE_LEN) != 0) { changed = true; strncpy(text[i], want[i], LINE_LEN); }
    if (!changed || !ok) return;
    d->clearBuffer();
    for (int i = 0; i < lines; i++) d->drawUTF8(0, i * line_h + ascent, text[i]);
    chunk = 0;  // send the new frame, piece by piece
  }

  // Sends one piece of a pending frame.
  void update() {
    if (!ok || chunk < 0) return;
    int per_row = (tiles_w + 7) / 8;
    int ty = chunk / per_row, tx = (chunk % per_row) * 8;
    d->updateDisplayArea(tx, ty, tiles_w - tx < 8 ? tiles_w - tx : 8, 1);
    if (++chunk >= chunks) chunk = -1;
  }
};

}  // namespace plc_oled
