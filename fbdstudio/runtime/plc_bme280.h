// FBD Studio: Bosch BME280 (temperature, humidity, pressure) and BMP280
// (temperature, pressure) over I2C, for the BME280 block.
//
// The sensor runs in "normal" mode and measures on its own every 125 ms; the
// block only reads the result registers (one 8-byte read), so a scan stays fast.
// Compensation: the floating-point formulas of Bosch's reference driver
// (BME280_SensorAPI, incl. its value limits); the FBD Studio tests compare
// these functions with that driver.
#pragma once
#include <stdint.h>
#include <string.h>

namespace plc_bme280 {

struct Calib {
  uint16_t T1; int16_t T2, T3;
  uint16_t P1; int16_t P2, P3, P4, P5, P6, P7, P8, P9;
  uint8_t H1; int16_t H2; uint8_t H3; int16_t H4, H5; int8_t H6;
};

// ---- pure functions (no I/O), also compiled by the tests on the PC

// buf88: 26 bytes from register 0x88; bufE1: 7 bytes from 0xE1 (BME280 only); H1 = register 0xA1
inline void parse_calib(const uint8_t* buf88, const uint8_t* bufE1, Calib& c) {
#define U16(b, i) (uint16_t)((b)[i] | ((b)[(i) + 1] << 8))
  c.T1 = U16(buf88, 0); c.T2 = (int16_t)U16(buf88, 2); c.T3 = (int16_t)U16(buf88, 4);
  c.P1 = U16(buf88, 6);
  c.P2 = (int16_t)U16(buf88, 8);  c.P3 = (int16_t)U16(buf88, 10); c.P4 = (int16_t)U16(buf88, 12);
  c.P5 = (int16_t)U16(buf88, 14); c.P6 = (int16_t)U16(buf88, 16); c.P7 = (int16_t)U16(buf88, 18);
  c.P8 = (int16_t)U16(buf88, 20); c.P9 = (int16_t)U16(buf88, 22);
  c.H1 = buf88[25];  // 0xA1
  if (bufE1) {
    c.H2 = (int16_t)U16(bufE1, 0);
    c.H3 = bufE1[2];
    c.H4 = (int16_t)(((int16_t)(int8_t)bufE1[3] * 16) | (bufE1[4] & 0x0F));
    c.H5 = (int16_t)(((int16_t)(int8_t)bufE1[5] * 16) | (bufE1[4] >> 4));
    c.H6 = (int8_t)bufE1[6];
  } else {
    c.H2 = 0; c.H3 = 0; c.H4 = 0; c.H5 = 0; c.H6 = 0;
  }
#undef U16
}

// d: 8 bytes from register 0xF7 (press[3], temp[3], hum[2])
inline void parse_raw(const uint8_t* d, int32_t& adc_p, int32_t& adc_t, int32_t& adc_h) {
  adc_p = ((int32_t)d[0] << 12) | ((int32_t)d[1] << 4) | (d[2] >> 4);
  adc_t = ((int32_t)d[3] << 12) | ((int32_t)d[4] << 4) | (d[5] >> 4);
  adc_h = ((int32_t)d[6] << 8) | d[7];
}

// returns °C (-40..85); t_fine is needed by the other two
inline double temperature(const Calib& c, int32_t adc_t, int32_t& t_fine) {
  double v1 = ((double)adc_t / 16384.0 - (double)c.T1 / 1024.0) * (double)c.T2;
  double d = (double)adc_t / 131072.0 - (double)c.T1 / 8192.0;
  double v2 = d * d * (double)c.T3;
  t_fine = (int32_t)(v1 + v2);
  double t = (v1 + v2) / 5120.0;
  return t < -40.0 ? -40.0 : (t > 85.0 ? 85.0 : t);
}

// returns Pa (30000..110000)
inline double pressure(const Calib& c, int32_t adc_p, int32_t t_fine) {
  double v1 = (double)t_fine / 2.0 - 64000.0;
  double v2 = v1 * v1 * (double)c.P6 / 32768.0;
  v2 = v2 + v1 * (double)c.P5 * 2.0;
  v2 = v2 / 4.0 + (double)c.P4 * 65536.0;
  v1 = ((double)c.P3 * v1 * v1 / 524288.0 + (double)c.P2 * v1) / 524288.0;
  v1 = (1.0 + v1 / 32768.0) * (double)c.P1;
  if (!(v1 > 0.0)) return 30000.0;  // invalid calibration: avoid division by zero
  double p = 1048576.0 - (double)adc_p;
  p = (p - v2 / 4096.0) * 6250.0 / v1;
  v1 = (double)c.P9 * p * p / 2147483648.0;
  v2 = p * (double)c.P8 / 32768.0;
  p = p + (v1 + v2 + (double)c.P7) / 16.0;
  return p < 30000.0 ? 30000.0 : (p > 110000.0 ? 110000.0 : p);
}

// returns %RH (0..100)
inline double humidity(const Calib& c, int32_t adc_h, int32_t t_fine) {
  double v1 = (double)t_fine - 76800.0;
  double v3 = (double)adc_h - ((double)c.H4 * 64.0 + (double)c.H5 / 16384.0 * v1);
  double v5 = 1.0 + (double)c.H3 / 67108864.0 * v1;
  double v6 = 1.0 + (double)c.H6 / 67108864.0 * v1 * v5;
  double h = v3 * ((double)c.H2 / 65536.0) * (v5 * v6);
  h = h * (1.0 - (double)c.H1 * h / 524288.0);
  return h > 100.0 ? 100.0 : (h < 0.0 ? 0.0 : h);
}

}  // namespace plc_bme280

#ifdef ARDUINO
#include "plc_i2c.h"

namespace plc_bme280 {

static const uint8_t CHIP_BME280 = 0x60, CHIP_BMP280 = 0x58;

struct Sensor {
  uint8_t addr = 0x76;
  uint8_t chip = 0;  // 0 = not (yet) found
  Calib cal;

  // Reads the chip id and calibration and starts continuous measuring.
  bool start() {
    uint8_t id;
    chip = 0;
    if (!plc_i2c::read(addr, 0xD0, &id, 1)) return false;
    if (id != CHIP_BME280 && id != CHIP_BMP280) return false;
    // soft reset: the sensor may still be running from before an ESP32 restart,
    // and settings are only accepted in sleep mode
    uint8_t v = 0xB6;
    if (!plc_i2c::write(addr, 0xE0, &v, 1)) return false;
    delay(3);
    for (int i = 0; i < 10; i++) {  // wait until the calibration data is copied from NVM
      uint8_t st;
      if (plc_i2c::read(addr, 0xF3, &st, 1) && !(st & 0x01)) break;
      delay(1);
    }
    uint8_t b88[26], bE1[7];
    if (!plc_i2c::read(addr, 0x88, b88, 26)) return false;
    bool has_h = id == CHIP_BME280;
    if (has_h && !plc_i2c::read(addr, 0xE1, bE1, 7)) return false;
    parse_calib(b88, has_h ? bE1 : nullptr, cal);
    if (has_h) { v = 0x01; if (!plc_i2c::write(addr, 0xF2, &v, 1)) return false; }  // humidity x1 (before 0xF4!)
    v = 0x40;  // standby 125 ms, IIR filter off
    if (!plc_i2c::write(addr, 0xF5, &v, 1)) return false;
    v = (1 << 5) | (1 << 2) | 0x03;  // temperature x1, pressure x1, normal mode
    if (!plc_i2c::write(addr, 0xF4, &v, 1)) return false;
    chip = id;
    return true;
  }

  // T in °C, H in %RH (0 for BMP280), P in hPa
  bool read(float& T, float& H, float& P) {
    uint8_t d[8];
    if (!plc_i2c::read(addr, 0xF7, d, chip == CHIP_BME280 ? 8 : 6)) return false;
    int32_t ap, at, ah = 0;
    if (chip != CHIP_BME280) { d[6] = 0; d[7] = 0; }
    parse_raw(d, ap, at, ah);
    if (at == 0x80000) return false;  // "no measurement yet" value after reset
    int32_t tf;
    T = (float)temperature(cal, at, tf);
    P = (float)(pressure(cal, ap, tf) / 100.0);
    H = chip == CHIP_BME280 ? (float)humidity(cal, ah, tf) : 0.0f;
    return true;
  }
};

}  // namespace plc_bme280
#endif
