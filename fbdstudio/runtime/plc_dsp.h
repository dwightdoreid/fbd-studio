// FBD Studio: signal processing helpers for the "Signal processing" blocks.
// C++11, no dynamic memory. Samples are taken once per scan cycle, so the sample
// rate is the scan rate; it is measured (Rate) rather than assumed.
#pragma once
#include <math.h>
#include <stdint.h>

namespace plc_dsp {

static const double kTwoPi = 6.283185307179586;

// Time between scans and a smoothed sample-rate estimate.
struct Rate {
  uint32_t last = 0;
  uint8_t scans = 0;  // 0: first scan, 1: one interval seen, 2: running
  float dt = 0.0f;    // seconds since the previous scan (0 on the first scan)
  float avg = 0.0f;   // smoothed scan interval

  float update(uint32_t now_us) {
    if (scans == 0) { dt = 0.0f; scans = 1; last = now_us; return dt; }
    dt = (float)(uint32_t)(now_us - last) * 1e-6f;
    last = now_us;
    if (dt <= 0.0f) return dt;
    if (scans == 1) { avg = dt; scans = 2; }
    else avg += 0.02f * (dt - avg);
    return dt;
  }
  bool ready() const { return scans == 2 && avg > 0.0f; }
  float fs() const { return avg > 0.0f ? 1.0f / avg : 0.0f; }
};

// ------------------------------------------------------------------ biquad
enum FilterType { LOWPASS = 0, HIGHPASS = 1, BANDPASS = 2, NOTCH = 3, ALLPASS = 4 };

// One second-order section, transposed direct form II, in double precision so that
// low cut-off frequencies (fc << fs) stay accurate.
struct Biquad {
  double b0 = 1, b1 = 0, b2 = 0, a1 = 0, a2 = 0;
  double z1 = 0, z2 = 0;

  double run(double x) {
    double y = b0 * x + z1;
    z1 = b1 * x - a1 * y + z2;
    z2 = b2 * x - a2 * y;
    return y;
  }
  double dc_gain() const {
    double d = 1.0 + a1 + a2;
    return fabs(d) < 1e-15 ? 0.0 : (b0 + b1 + b2) / d;
  }
  // set the state as if x had been applied for ever (no start-up transient)
  void settle(double x) {
    double y = dc_gain() * x;
    z2 = b2 * x - a2 * y;
    z1 = y - b0 * x;
  }
  // Coefficients from the Audio EQ Cookbook (R. Bristow-Johnson).
  void design(int type, double fc, double q, double fs) {
    if (fs <= 0) return;
    if (fc < 1e-6 * fs) fc = 1e-6 * fs;
    if (fc > 0.49 * fs) fc = 0.49 * fs;
    if (q < 0.05) q = 0.05;
    double w = kTwoPi * fc / fs, c = cos(w), al = sin(w) / (2.0 * q);
    double n0, n1, n2;
    switch (type) {
      case HIGHPASS: n0 = (1 + c) / 2; n1 = -(1 + c); n2 = (1 + c) / 2; break;
      case BANDPASS: n0 = al; n1 = 0; n2 = -al; break;
      case NOTCH: n0 = 1; n1 = -2 * c; n2 = 1; break;
      case ALLPASS: n0 = 1 - al; n1 = -2 * c; n2 = 1 + al; break;
      default: n0 = (1 - c) / 2; n1 = 1 - c; n2 = (1 - c) / 2; break;
    }
    double a0 = 1 + al;
    b0 = n0 / a0; b1 = n1 / a0; b2 = n2 / a0;
    a1 = -2 * c / a0; a2 = (1 - al) / a0;
  }
};

// DC gain of a designed filter type (known before the sample rate is).
inline float dc_gain_of(int type) { return (type == HIGHPASS || type == BANDPASS) ? 0.0f : 1.0f; }

// Q of section k (0-based) of a Butterworth filter of the given even order.
inline double butterworth_q(int order, int k) {
  return 1.0 / (2.0 * cos((2 * k + 1) * 3.141592653589793 / (2.0 * order)));
}

// ------------------------------------------------------------------ FFT
enum Window { RECT = 0, HANN = 1, HAMMING = 2, FLATTOP = 3 };

inline float window(int type, int i, int n) {
  double x = kTwoPi * i / n;  // periodic window (for spectral analysis)
  switch (type) {
    case HANN: return (float)(0.5 - 0.5 * cos(x));
    case HAMMING: return (float)(0.54 - 0.46 * cos(x));
    case FLATTOP:
      return (float)(0.21557895 - 0.41663158 * cos(x) + 0.277263158 * cos(2 * x) - 0.083578947 * cos(3 * x) +
                     0.006947368 * cos(4 * x));
    default: return 1.0f;
  }
}

// In-place radix-2 FFT, n a power of two.
inline void fft(float* re, float* im, int n) {
  for (int i = 1, j = 0; i < n; i++) {
    int bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      float t = re[i]; re[i] = re[j]; re[j] = t;
      t = im[i]; im[i] = im[j]; im[j] = t;
    }
  }
  for (int len = 2; len <= n; len <<= 1) {
    double ang = -kTwoPi / len;
    float wr = (float)cos(ang), wi = (float)sin(ang);
    for (int i = 0; i < n; i += len) {
      float cr = 1.0f, ci = 0.0f;
      for (int k = 0; k < len / 2; k++) {
        int a = i + k, b = a + len / 2;
        float xr = re[b] * cr - im[b] * ci, xi = re[b] * ci + im[b] * cr;
        re[b] = re[a] - xr; im[b] = im[a] - xi;
        re[a] += xr; im[a] += xi;
        float t = cr * wr - ci * wi;
        ci = cr * wi + ci * wr;
        cr = t;
      }
    }
  }
}

static const int FFT_MAX = 512;

struct Spectrum {
  float peak_f = 0, peak_a = 0, mag = 0, band_rms = 0, rms = 0, dc = 0;
};

// Analyses n samples x taken at fs. Amplitudes are single-sided peak values (a sine
// of amplitude A gives A), rms is the AC RMS value of the whole frame.
inline Spectrum analyse(const float* x, int n, float fs, int win, float f_mag, float f_lo, float f_hi) {
  static float re[FFT_MAX], im[FFT_MAX];  // scratch shared by all FFT blocks (they run one after another)
  Spectrum s;
  if (n > FFT_MAX) n = FFT_MAX;
  double mean = 0, sum_w2 = 0;
  for (int i = 0; i < n; i++) mean += x[i];
  mean /= n;
  for (int i = 0; i < n; i++) {
    float w = window(win, i, n);
    sum_w2 += (double)w * w;
    re[i] = (float)((x[i] - mean) * w);
    im[i] = 0.0f;
  }
  fft(re, im, n);
  s.dc = (float)mean;
  double pow_scale = 2.0 / (n * sum_w2);    // |X_k|^2 -> mean square (Parseval with the window)
  double df = fs / n, total = 0, band = 0;
  int half = n / 2, kmax = 1;
  double pmax = -1.0;
  for (int k = 1; k < half; k++) {
    double p = (double)re[k] * re[k] + (double)im[k] * im[k];
    total += p;
    double f = k * df;
    if (f >= f_lo && f <= f_hi) band += p;
    if (p > pmax) { pmax = p; kmax = k; }
  }
  s.rms = (float)sqrt(total * pow_scale);
  s.band_rms = (float)sqrt(band * pow_scale);
  // Frequency: power-weighted centre of the bins around the peak; amplitude: the energy
  // of those bins (Parseval). Within +-3 bins this is accurate to ~0.005 bins and 0.1 %
  // for the Hann, Hamming and flat-top windows, wherever the frequency falls between bins.
  double pk = 0, pp = 0;
  for (int k = kmax - 3; k <= kmax + 3; k++) {
    if (k < 1 || k >= half) continue;
    double p = (double)re[k] * re[k] + (double)im[k] * im[k];
    pk += k * p; pp += p;
  }
  s.peak_f = pp > 0 ? (float)(pk / pp * df) : 0.0f;
  s.peak_a = (float)sqrt(2.0 * pow_scale * pp);
  int km = (int)(f_mag / df + 0.5);
  if (km == 0) s.mag = (float)fabs(mean);
  else if (km < half) {
    double pm = 0;
    for (int k = km - 3; k <= km + 3; k++)
      if (k >= 1 && k < half) pm += (double)re[k] * re[k] + (double)im[k] * im[k];
    s.mag = (float)sqrt(2.0 * pow_scale * pm);
  }
  return s;
}


}  // namespace plc_dsp
