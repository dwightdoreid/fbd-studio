// FBD Studio: text <-> value conversion for communication blocks (MQTT, Bluetooth).
// C++11, no dynamic memory.
#pragma once
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

namespace plc_text {

// bool_style: 0 = "1"/"0", 1 = "ON"/"OFF", 2 = "true"/"false"
inline void format(bool v, char* out, size_t n, int /*decimals*/, int bool_style) {
  static const char* const T[] = {"1", "ON", "true"};
  static const char* const F[] = {"0", "OFF", "false"};
  int s = bool_style < 0 || bool_style > 2 ? 0 : bool_style;
  snprintf(out, n, "%s", v ? T[s] : F[s]);
}
inline void format(int16_t v, char* out, size_t n, int, int) { snprintf(out, n, "%d", (int)v); }
inline void format(int32_t v, char* out, size_t n, int, int) { snprintf(out, n, "%ld", (long)v); }
inline void format(float v, char* out, size_t n, int decimals, int) {
  if (v != v) { snprintf(out, n, "nan"); return; }
  snprintf(out, n, "%.*f", decimals < 0 ? 0 : (decimals > 6 ? 6 : decimals), (double)v);
}

inline const char* skip_ws(const char* s) { while (*s && isspace((unsigned char)*s)) s++; return s; }

inline bool ieq(const char* a, const char* b) {
  a = skip_ws(a);
  while (*b) { if (tolower((unsigned char)*a) != tolower((unsigned char)*b)) return false; a++; b++; }
  return *skip_ws(a) == 0;
}

// Accepts 1/0, on/off, true/false, high/low (any case) and any number (!= 0 is TRUE).
inline bool parse(const char* s, bool& v) {
  if (ieq(s, "1") || ieq(s, "on") || ieq(s, "true") || ieq(s, "high")) { v = true; return true; }
  if (ieq(s, "0") || ieq(s, "off") || ieq(s, "false") || ieq(s, "low")) { v = false; return true; }
  char* end; double d = strtod(s, &end);
  if (end == s || *skip_ws(end)) return false;
  v = d != 0; return true;
}
inline bool parse_num(const char* s, double& d) {
  char* end; d = strtod(s, &end);
  return end != s && *skip_ws(end) == 0 && d == d;
}
inline bool parse(const char* s, int16_t& v) {
  double d; bool b;
  if (!parse_num(s, d)) { if (!parse(s, b)) return false; d = b; }
  d = d < -32768.0 ? -32768.0 : (d > 32767.0 ? 32767.0 : d);
  v = (int16_t)(d < 0 ? d - 0.5 : d + 0.5); return true;
}
inline bool parse(const char* s, int32_t& v) {
  double d; bool b;
  if (!parse_num(s, d)) { if (!parse(s, b)) return false; d = b; }
  d = d < -2147483648.0 ? -2147483648.0 : (d > 2147483647.0 ? 2147483647.0 : d);
  v = (int32_t)(d < 0 ? d - 0.5 : d + 0.5); return true;
}
inline bool parse(const char* s, float& v) {
  double d; bool b;
  if (!parse_num(s, d)) { if (!parse(s, b)) return false; d = b; }
  v = (float)d; return true;
}

// ---- display line templates
// "Temp {1:1} C" -> "Temp 21.5 C": {n} is value n (1-based) with 'decimals'
// decimals, {n:d} with d decimals (0..6). "{{" gives a literal "{".
inline void render(const char* tmpl, const float* values, int nvalues, int decimals, char* out, size_t outn) {
  size_t o = 0;
  if (outn == 0) return;
  for (const char* p = tmpl; *p && o + 1 < outn; p++) {
    if (p[0] == '{' && p[1] == '{') { out[o++] = '{'; p++; continue; }
    if (p[0] == '{' && p[1] >= '1' && p[1] <= '9') {
      const char* q = p + 1;
      int idx = 0;
      while (*q >= '0' && *q <= '9') idx = idx * 10 + (*q++ - '0');
      int dec = decimals;
      if (*q == ':' && q[1] >= '0' && q[1] <= '9') { dec = q[1] - '0'; q += 2; }
      if (*q == '}' && idx >= 1 && idx <= nvalues) {
        char num[24];
        format(values[idx - 1], num, sizeof num, dec, 0);
        for (const char* c = num; *c && o + 1 < outn; c++) out[o++] = *c;
        p = q;
        continue;
      }
    }
    out[o++] = *p;
  }
  out[o] = 0;
}

// ---- bytes <-> integer (for I2C / SPI registers)
inline int32_t unpack(const uint8_t* b, int n, bool msb_first, bool is_signed) {
  uint32_t u = 0;
  for (int i = 0; i < n; i++) u |= (uint32_t)b[msb_first ? i : n - 1 - i] << (8 * (n - 1 - i));
  if (is_signed && n < 4 && (u & (1UL << (8 * n - 1)))) u |= ~((1UL << (8 * n)) - 1);  // sign-extend
  return (int32_t)u;
}
inline void pack(int32_t v, uint8_t* b, int n, bool msb_first) {
  uint32_t u = (uint32_t)v;
  for (int i = 0; i < n; i++) b[msb_first ? i : n - 1 - i] = (uint8_t)(u >> (8 * (n - 1 - i)));
}

}  // namespace plc_text
