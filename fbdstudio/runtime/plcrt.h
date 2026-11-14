// FBD Studio runtime: scan cycle timing, online monitoring and helpers.
// Header-only, C++11, uses only the Arduino core API.
//
// Monitor protocol (text lines over Serial):
//   device -> host
//     #FBD 1 <hash> <symbol count>        hello (sent at start-up and on '?')
//     =<ms> <scan us> <max scan us> v0 v1 ...   values of all symbols
//     !W <idx> / !E <message>             write acknowledged / error
//   host -> device
//     ?                 request hello
//     W <idx> <value>   write a writable symbol (an unconnected input)
//     P <ms>            set monitoring period (0 = off)
//     R                 reset max scan time
#pragma once
#include <Arduino.h>
#include <stdint.h>
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

// ------------------------------------------------------------------ runtime state
struct Runtime {
  const Sym* syms;
  uint16_t nsyms;
  const char* hash;
  uint32_t cycle_us;
  uint32_t monitor_ms;
  uint32_t t_cycle;
  uint32_t t_monitor;
  uint32_t scan_us;
  uint32_t scan_max;
  char buf[48];
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

inline void printSym(const Sym& s) {
  switch (s.type) {
    case T_BOOL: Serial.print(*(const bool*)s.ptr ? 1 : 0); break;
    case T_INT: Serial.print((int)*(const int16_t*)s.ptr); break;
    case T_DINT:
    case T_TIME: Serial.print((long)*(const int32_t*)s.ptr); break;
    case T_REAL: Serial.print((double)*(const float*)s.ptr, 4); break;
  }
}

inline void sendValues() {
  Runtime& r = rt();
  Serial.print('=');
  Serial.print((unsigned long)millis());
  Serial.print(' ');
  Serial.print((unsigned long)r.scan_us);
  Serial.print(' ');
  Serial.print((unsigned long)r.scan_max);
  for (uint16_t i = 0; i < r.nsyms; i++) {
    Serial.print(' ');
    printSym(r.syms[i]);
  }
  Serial.println();
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

inline void handleLine(char* line) {
  Runtime& r = rt();
  switch (line[0]) {
    case '?': hello(); break;
    case 'W': writeSym(line + 1); break;
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
  Serial.begin(baud);
  r.t_monitor = millis();
  hello();
}

inline void cycleBegin() { rt().t_cycle = micros(); }

inline void cycleEnd() {
  Runtime& r = rt();
  r.scan_us = micros() - r.t_cycle;
  if (r.scan_us > r.scan_max) r.scan_max = r.scan_us;
  pollSerial();
  if (r.monitor_ms && (uint32_t)(millis() - r.t_monitor) >= r.monitor_ms) {
    r.t_monitor = millis();
    sendValues();
  }
  if (r.cycle_us) {
    while ((uint32_t)(micros() - r.t_cycle) < r.cycle_us) yield();
  }
}

}  // namespace plc
