// FBD Studio runtime: scan cycle timing, online monitoring and helpers.
// Header-only, C++11, uses only the Arduino core API.
//
// Monitor protocol (text lines over Serial):
//   device -> host
//     #FBD 1 <hash> <symbol count>        hello (sent at start-up and on '?')
//     =<ms> <scan us> <max scan us> v0 v1 ...   values of all symbols
//     !W <idx> / !E <message>             write acknowledged / error
//     %A <len> <channels>                 capture armed
//     %T                                  capture triggered
//     %C <len> <channels> <period us> <pre>   capture data follows: <len> lines of
//     %D v0 v1 ...                        one sample of each channel, then
//     %E                                  end of the capture data
//   host -> device
//     ?                 request hello
//     W <idx> <value>   write a writable symbol (an unconnected input)
//     P <ms>            set monitoring period (0 = off)
//     R                 reset max scan time
//     C <every> <len> <pre> <trig> <edge> <level> <i0> [<i1> ...]
//                       capture: record symbols i0.. (max. 8) every <every> scans,
//                       <len> samples, <pre> of them before the trigger. <trig> is
//                       the trigger symbol (-1 = start at once), <edge> 1 = rising,
//                       2 = falling, 3 = both, through <level>.
//     C                 cancel a capture
#pragma once
#include <Arduino.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <limits>
#include <type_traits>

namespace plc {

enum : uint8_t { T_BOOL = 0, T_INT = 1, T_DINT = 2, T_REAL = 3, T_TIME = 4 };

struct Sym {
  void* ptr;
  uint8_t type;
  uint8_t writable;
};

// ------------------------------------------------------------------ conversions
template <typename D>
inline D saturate(long long v) {
  if (v < (long long)std::numeric_limits<D>::min()) return std::numeric_limits<D>::min();
  if (v > (long long)std::numeric_limits<D>::max()) return std::numeric_limits<D>::max();
  return (D)v;
}

template <typename D, typename S>
inline D conv_(S v, std::true_type /*floating source*/) {
  if (v != v) return 0;  // NaN
  double r = (double)v;
  r = r < 0 ? r - 0.5 : r + 0.5;  // round half away from zero
  if (r <= (double)std::numeric_limits<D>::min()) return std::numeric_limits<D>::min();
  if (r >= (double)std::numeric_limits<D>::max()) return std::numeric_limits<D>::max();
  return (D)(long long)r;
}

template <typename D, typename S>
inline D conv_(S v, std::false_type) {
  return saturate<D>((long long)v);
}

// Convert to an integer type with rounding (from REAL) and saturation.
template <typename D, typename S>
inline D conv(S v) {
  return conv_<D>(v, typename std::is_floating_point<S>::type());
}

// ------------------------------------------------------------------ serial transmit buffer
// Without a software buffer, Serial.print() waits whenever the 128-byte UART FIFO is
// full: one monitor line (~100 characters at 115200 baud) would stall the scan for
// ~9 ms. With a buffer, printing returns at once and the UART sends in the background.
#ifndef PLC_TX_BUFFER
#define PLC_TX_BUFFER 2048
#endif

// Serial.setTxBufferSize() exists on the ESP32 UART and HW-CDC ports only; elsewhere
// this does nothing.
template <typename S>
inline auto setTxBuffer(S& s, int) -> decltype(s.setTxBufferSize((size_t)0), void()) { s.setTxBufferSize(PLC_TX_BUFFER); }
template <typename S>
inline void setTxBuffer(S&, long) {}

// ------------------------------------------------------------------ capture (scope)
#ifndef PLC_CAPTURE_MAX
#define PLC_CAPTURE_MAX 4096  // values (samples x channels) the capture buffer holds
#endif
#define PLC_CAPTURE_CH 8

struct Capture {
  enum : uint8_t { IDLE, PREFILL, ARMED, POST, SEND };
  uint8_t state = IDLE;
  uint8_t nch = 0;
  uint16_t ch[PLC_CAPTURE_CH];
  int16_t trig = -1;
  uint8_t edge = 1;
  float level = 0, prev = 0;
  uint16_t len = 0, pre = 0, every = 1, skip = 0;
  uint16_t head = 0, filled = 0, left = 0, sent = 0;
  uint32_t nrec = 0, t_first = 0, period_us = 0;
  float buf[PLC_CAPTURE_MAX];
};

inline Capture& cap() {
  static Capture c;
  return c;
}

// ------------------------------------------------------------------ runtime state
struct Runtime {
  const Sym* syms;
  uint16_t nsyms;
  const char* hash;
  uint32_t cycle_us;
  uint32_t monitor_ms;
  uint32_t t_cycle;
  uint32_t t_next;  // planned start of the next scan (fixed rate, no drift)
  uint32_t t_monitor;
  uint32_t scan_us;
  uint32_t scan_max;
  char buf[160];  // longest command: a capture with 8 channels
  uint8_t len;
};

inline Runtime& rt() {
  static Runtime r;
  return r;
}

inline void hello() {
  Runtime& r = rt();
  Serial.print("#FBD 1 ");
  Serial.print(r.hash);
  Serial.print(' ');
  Serial.println((unsigned long)r.nsyms);
}

// Formats one value as the monitor protocol sends it; returns the number of characters.
inline int formatSym(const Sym& s, char* out, size_t n) {
  switch (s.type) {
    case T_BOOL: return snprintf(out, n, "%d", *(const bool*)s.ptr ? 1 : 0);
    case T_INT: return snprintf(out, n, "%d", (int)*(const int16_t*)s.ptr);
    case T_REAL: {
      double v = *(const float*)s.ptr;
      return snprintf(out, n, (v > -1e9 && v < 1e9) ? "%.4f" : "%.6g", v);
    }
    default: return snprintf(out, n, "%ld", (long)*(const int32_t*)s.ptr);
  }
}

// The values line is built in a buffer and written in a few large pieces: printing
// it value by value (Serial.print(double)) hands every character to the UART driver
// separately, which took ~15 ms per line on an ESP32 and stretched the scan cycle.
inline void sendValues() {
  Runtime& r = rt();
  char out[256];
  int k = snprintf(out, sizeof(out), "=%lu %lu %lu", (unsigned long)millis(), (unsigned long)r.scan_us,
                   (unsigned long)r.scan_max);
  for (uint16_t i = 0; i < r.nsyms; i++) {
    if (k > (int)sizeof(out) - 40) {  // flush before a value could overflow the buffer
      out[k] = 0;
      Serial.print(out);
      k = 0;
    }
    out[k++] = ' ';
    int m = formatSym(r.syms[i], out + k, sizeof(out) - k);
    if (m > 0) k += m < (int)sizeof(out) - k ? m : (int)sizeof(out) - k - 1;
  }
  out[k] = 0;
  Serial.println(out);
}

inline void writeSym(char* args) {
  Runtime& r = rt();
  char* end;
  long idx = strtol(args, &end, 10);
  if (end == args || idx < 0 || idx >= (long)r.nsyms) { Serial.println("!E bad index"); return; }
  const Sym& s = r.syms[idx];
  if (!s.writable) { Serial.println("!E not writable"); return; }
  char* val = end;
  while (*val == ' ') val++;
  switch (s.type) {
    case T_BOOL: *(bool*)s.ptr = atol(val) != 0; break;
    case T_INT: *(int16_t*)s.ptr = saturate<int16_t>(atol(val)); break;
    case T_DINT:
    case T_TIME: *(int32_t*)s.ptr = (int32_t)strtol(val, 0, 10); break;
    case T_REAL: *(float*)s.ptr = (float)atof(val); break;
  }
  Serial.print("!W ");
  Serial.println(idx);
}

inline float symValue(const Sym& s) {
  switch (s.type) {
    case T_BOOL: return *(const bool*)s.ptr ? 1.0f : 0.0f;
    case T_INT: return (float)*(const int16_t*)s.ptr;
    case T_REAL: return *(const float*)s.ptr;
    default: return (float)*(const int32_t*)s.ptr;
  }
}

inline void startCapture(char* args) {
  Runtime& r = rt();
  Capture& c = cap();
  char* p = args;
  long v[6];
  for (int i = 0; i < 6; i++) {
    char* end;
    if (i == 5) {
      c.level = (float)strtod(p, &end);
    } else {
      v[i] = strtol(p, &end, 10);
    }
    if (end == p) { c.state = Capture::IDLE; if (i) Serial.println("!E capture: bad arguments"); return; }  // "C" alone cancels
    p = end;
  }
  c.nch = 0;
  for (;;) {
    char* end;
    long idx = strtol(p, &end, 10);
    if (end == p) break;
    p = end;
    if (idx < 0 || idx >= (long)r.nsyms || c.nch >= PLC_CAPTURE_CH) { Serial.println("!E capture: bad channel"); return; }
    c.ch[c.nch++] = (uint16_t)idx;
  }
  if (!c.nch) { Serial.println("!E capture: no channels"); return; }
  long maxlen = PLC_CAPTURE_MAX / c.nch;
  c.every = (uint16_t)(v[0] < 1 ? 1 : (v[0] > 60000 ? 60000 : v[0]));
  c.len = (uint16_t)(v[1] < 2 ? 2 : (v[1] > maxlen ? maxlen : v[1]));
  c.trig = (int16_t)(v[3] < 0 || v[3] >= (long)r.nsyms ? -1 : v[3]);
  c.pre = (uint16_t)(c.trig < 0 || v[2] < 0 ? 0 : (v[2] >= c.len ? c.len - 1 : v[2]));
  c.edge = (uint8_t)(v[4] < 1 || v[4] > 3 ? 1 : v[4]);
  c.skip = 0; c.head = 0; c.filled = 0; c.nrec = 0; c.sent = 0;
  if (c.trig < 0) { c.state = Capture::POST; c.left = c.len; }
  else { c.state = c.pre ? Capture::PREFILL : Capture::ARMED; c.prev = symValue(r.syms[c.trig]); }
  Serial.print("%A ");
  Serial.print((unsigned long)c.len);
  Serial.print(' ');
  Serial.println((unsigned long)c.nch);
}

// Called once per scan, after the program ran.
inline void captureSample() {
  Capture& c = cap();
  if (c.state == Capture::IDLE || c.state == Capture::SEND) return;
  if (++c.skip < c.every) return;
  c.skip = 0;
  Runtime& r = rt();
  float* row = c.buf + (uint32_t)c.head * c.nch;
  for (uint8_t i = 0; i < c.nch; i++) row[i] = symValue(r.syms[c.ch[i]]);
  c.head = (uint16_t)((c.head + 1) % c.len);
  if (c.filled < c.len) c.filled++;
  uint32_t now = micros();
  if (c.nrec++ == 0) c.t_first = now;
  switch (c.state) {
    case Capture::PREFILL:
      if (c.filled >= c.pre) c.state = Capture::ARMED;
      c.prev = symValue(r.syms[c.trig]);
      break;
    case Capture::ARMED: {
      float x = symValue(r.syms[c.trig]);
      bool up = c.prev < c.level && x >= c.level, down = c.prev > c.level && x <= c.level;
      c.prev = x;
      if (((c.edge & 1) && up) || ((c.edge & 2) && down)) {
        c.left = (uint16_t)(c.len - c.pre - 1);  // this sample is the trigger sample
        c.state = Capture::POST;
        Serial.println("%T");
      }
      break;
    }
    default:
      if (c.left) c.left--;
      break;
  }
  if (c.state == Capture::POST && c.left == 0) {
    c.period_us = c.nrec > 1 ? (uint32_t)((now - c.t_first) / (c.nrec - 1)) : 0;
    c.state = Capture::SEND;
    c.sent = 0;
    Serial.print("%C ");
    Serial.print((unsigned long)c.len);
    Serial.print(' ');
    Serial.print((unsigned long)c.nch);
    Serial.print(' ');
    Serial.print((unsigned long)c.period_us);
    Serial.print(' ');
    Serial.println((unsigned long)c.pre);
  }
}

// Sends a few captured samples per scan, only as many as fit into the serial transmit
// buffer, so that sending never stretches the scan cycle.
inline void captureSend() {
  Capture& c = cap();
  if (c.state != Capture::SEND) return;
  char line[16 * PLC_CAPTURE_CH + 8];
  for (int n = 0; n < 16 && c.sent < c.len; n++) {
    uint16_t idx = (uint16_t)((c.head + c.sent) % c.len);  // the oldest sample first
    const float* row = c.buf + (uint32_t)idx * c.nch;
    int k = snprintf(line, sizeof(line), "%%D");
    for (uint8_t i = 0; i < c.nch; i++) {
      int m = snprintf(line + k, sizeof(line) - k, " %.6g", (double)row[i]);
      if (m > 0) k += m;
      if (k >= (int)sizeof(line)) k = (int)sizeof(line) - 1;
    }
    // Leave some room for the monitor line, so that it does not have to wait either.
    int room = Serial.availableForWrite();
    if (room > 0 ? room < k + 2 + 64 : n > 0) break;  // room 0: the port cannot tell, one line per scan
    Serial.println(line);
    c.sent++;
  }
  if (c.sent >= c.len) { Serial.println("%E"); c.state = Capture::IDLE; }
}

inline void handleLine(char* line) {
  Runtime& r = rt();
  switch (line[0]) {
    case '?': hello(); break;
    case 'W': writeSym(line + 1); break;
    case 'C': startCapture(line + 1); break;
    case 'P': r.monitor_ms = (uint32_t)atol(line + 1); break;
    case 'R': r.scan_max = 0; break;
    default: break;
  }
}

inline void pollSerial() {
  Runtime& r = rt();
  while (Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == '\n' || c == '\r') {
      if (r.len) {
        r.buf[r.len] = 0;
        handleLine(r.buf);
        r.len = 0;
      }
    } else if (r.len < sizeof(r.buf) - 1) {
      r.buf[r.len++] = (char)c;
    }
  }
}

// ------------------------------------------------------------------ scan cycle
inline void begin(const Sym* syms, uint16_t nsyms, const char* hash, unsigned long baud,
                  uint32_t cycle_ms, uint32_t monitor_ms) {
  Runtime& r = rt();
  r.syms = syms;
  r.nsyms = nsyms;
  r.hash = hash;
  r.cycle_us = cycle_ms * 1000UL;
  r.monitor_ms = monitor_ms;
  r.scan_us = r.scan_max = 0;
  r.len = 0;
  setTxBuffer(Serial, 0);
  Serial.begin(baud);
  r.t_monitor = millis();
  r.t_next = micros();
  hello();
}

inline void cycleBegin() { rt().t_cycle = micros(); }

inline void cycleEnd() {
  Runtime& r = rt();
  r.scan_us = micros() - r.t_cycle;
  if (r.scan_us > r.scan_max) r.scan_max = r.scan_us;
  captureSample();
  pollSerial();
  captureSend();
  if (r.monitor_ms && (uint32_t)(millis() - r.t_monitor) >= r.monitor_ms) {
    r.t_monitor = millis();
    sendValues();
  }
  if (r.cycle_us) {
    // Fixed-rate schedule: scans start every cycle_us on average, even if waiting
    // overshoots a little. After an overrun (a scan longer than the cycle time)
    // the schedule restarts instead of running the missed scans back to back.
    r.t_next += r.cycle_us;
    if ((int32_t)(micros() - r.t_next) > (int32_t)r.cycle_us) r.t_next = micros();
    while ((int32_t)(r.t_next - micros()) > 0) yield();
  }
}

}  // namespace plc
