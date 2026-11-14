// FBD Studio: "name=value" line protocol over Bluetooth, for BT_SEND / BT_RECV.
//
// The connection itself is provided by one transport block:
//   BT_SERIAL  classic Bluetooth serial (SPP): Android, PCs      -> plc_bt_classic.h
//   BLE_UART   Bluetooth Low Energy, Nordic UART Service: iPhone,
//              Android, PCs, ESP32/S3/C3                          -> plc_ble.h
//
// Protocol: text lines "name=value". BT_SEND blocks send their value when it
// changes (and when a client connects); BT_RECV blocks take values sent with
// their name ("name: value" works too). A line "?" asks for all values again.
#pragma once
#include <Arduino.h>
#include "plc_text.h"

namespace plc_bt {

static const int MAX_NAMES = 32;
static const int VAL_LEN = 32;
static const int LINE_LEN = 80;

// Implemented by the transport (classic or BLE).
struct Transport {
  bool (*connected)();
  int (*read)();  // next received byte, or -1
  void (*write)(const char* data, size_t len);
};

struct Slot {
  const char* name;
  bool rx;
  bool fresh;
  char val[VAL_LEN];
};

struct Ctx {
  const Transport* tr = nullptr;
  bool client = false;
  uint32_t epoch = 0;  // changes when a client connects or asks for all values
  Slot slots[MAX_NAMES];
  int n = 0;
  char line[LINE_LEN];
  int len = 0;
};
inline Ctx& ctx() { static Ctx c; return c; }

inline void set_transport(const Transport* t) { ctx().tr = t; }

inline int add(const char* name, bool rx) {
  Ctx& c = ctx();
  if (c.n >= MAX_NAMES) return -1;
  Slot& s = c.slots[c.n];
  s.name = name; s.rx = rx; s.fresh = false; s.val[0] = 0;
  return c.n++;
}

inline bool name_eq(const char* a, const char* b) {
  while (*a && *b) { if (tolower((unsigned char)*a) != tolower((unsigned char)*b)) return false; a++; b++; }
  return *a == 0 && *b == 0;
}

inline void handle_line(char* line) {
  Ctx& c = ctx();
  if (line[0] == '?' && line[1] == 0) { c.epoch++; return; }
  char* eq = strchr(line, '=');
  if (!eq) eq = strchr(line, ':');
  if (!eq) return;
  *eq = 0;
  char* name = line;
  while (*name == ' ') name++;
  for (char* e = eq - 1; e >= name && *e == ' '; e--) *e = 0;
  for (int i = 0; i < c.n; i++) {
    Slot& s = c.slots[i];
    if (s.rx && name_eq(s.name, name)) {
      snprintf(s.val, VAL_LEN, "%s", eq + 1);
      s.fresh = true;
    }
  }
}

// Reads incoming bytes. Cheap and safe to call several times per scan.
inline void poll() {
  Ctx& c = ctx();
  if (!c.tr) return;
  bool has = c.tr->connected();
  if (has && !c.client) c.epoch++;  // new connection: send everything
  c.client = has;
  for (int budget = 256; budget > 0; budget--) {  // bounded work per call
    int ch = c.tr->read();
    if (ch < 0) break;
    if (ch == '\n' || ch == '\r') {
      if (c.len) { c.line[c.len] = 0; handle_line(c.line); }
      c.len = 0;
    } else if (c.len < LINE_LEN - 1) {
      c.line[c.len++] = (char)ch;
    }
  }
}

inline bool connected() { return ctx().client; }
inline uint32_t epoch() { return ctx().epoch; }

inline void send(int idx, const char* text) {
  Ctx& c = ctx();
  if (idx < 0 || !c.client || !c.tr) return;
  char buf[LINE_LEN + VAL_LEN];
  int n = snprintf(buf, sizeof buf, "%s=%s\r\n", c.slots[idx].name, text);
  if (n > 0) c.tr->write(buf, (size_t)(n < (int)sizeof buf ? n : (int)sizeof buf - 1));
}

inline bool take(int idx, char* out, size_t n) {
  if (idx < 0) return false;
  Slot& s = ctx().slots[idx];
  if (!s.fresh) return false;
  snprintf(out, n, "%s", s.val);
  s.fresh = false;
  return true;
}

}  // namespace plc_bt
