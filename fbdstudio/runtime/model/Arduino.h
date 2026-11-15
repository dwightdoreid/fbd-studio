// FBD Studio: minimal Arduino API for the Modelica export.
//
// The generated program includes this header inside an anonymous namespace, so
// everything here has internal linkage: several exported controllers can be
// linked into one Modelica model without symbol clashes. The system headers it
// relies on (stdint.h, string.h, ...) are included by the generated file first.
//
// Time does not come from a clock but from the Modelica simulation: the wrapper
// sets fbd_now_us before every scan, so timers run in simulated time.
#pragma once

#define HIGH 1
#define LOW 0
#define INPUT 0x01
#define OUTPUT 0x03
#define INPUT_PULLUP 0x05
#define INPUT_PULLDOWN 0x09
#define LSBFIRST 0
#define MSBFIRST 1
#define SPI_MODE0 0
#define SPI_MODE1 1
#define SPI_MODE2 2
#define SPI_MODE3 3

static uint64_t fbd_now_us = 0;  // simulated time in µs, set by the wrapper

static inline unsigned long millis() { return (unsigned long)(uint32_t)(fbd_now_us / 1000u); }
static inline unsigned long micros() { return (unsigned long)(uint32_t)fbd_now_us; }
static inline void delay(unsigned long) {}
static inline void delayMicroseconds(unsigned int) {}
static inline void yield() {}
static inline void noInterrupts() {}
static inline void interrupts() {}

// I/O blocks are replaced by Modelica connectors; these only keep user blocks compiling.
static inline void pinMode(int, int) {}
static inline void digitalWrite(int, int) {}
static inline int digitalRead(int) { return LOW; }
static inline int analogRead(int) { return 0; }
static inline void analogWrite(int, int) {}

// Serial output goes nowhere in a Modelica simulation.
struct ModelSerial {
  void begin(unsigned long) {}
  int available() { return 0; }
  int read() { return -1; }
  void flush() {}
  size_t write(char) { return 1; }
  size_t print(const char*) { return 0; }
  size_t print(char) { return 0; }
  size_t print(int) { return 0; }
  size_t print(unsigned int) { return 0; }
  size_t print(long) { return 0; }
  size_t print(unsigned long) { return 0; }
  size_t print(double, int = 2) { return 0; }
  size_t println() { return 0; }
  template <typename X>
  size_t println(X) { return 0; }
  size_t println(double, int) { return 0; }
  operator bool() const { return true; }
};
static ModelSerial Serial;
