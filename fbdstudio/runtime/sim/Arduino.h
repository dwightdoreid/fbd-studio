// Minimal Arduino API emulation used by the FBD Studio simulator.
// The generated program is compiled natively on the PC against this header.
#pragma once
#include <stdint.h>
#include <stdlib.h>
#include <math.h>
#include <string.h>

#define HIGH 1
#define LOW 0
#define INPUT 0x01
#define OUTPUT 0x03
#define INPUT_PULLUP 0x05
#define INPUT_PULLDOWN 0x09

#define LSBFIRST 0
#define MSBFIRST 1
// From SPI.h: used as parameter values by SPI blocks (SPI itself is not simulated).
#define SPI_MODE0 0
#define SPI_MODE1 1
#define SPI_MODE2 2
#define SPI_MODE3 3

// Math constants from the real Arduino.h. Defined here too, so that a block that clashes
// with them (e.g. a variable named TWO_PI) fails in the simulator and the tests as well.
#define PI 3.1415926535897932384626433832795
#define HALF_PI 1.5707963267948966192313216916398
#define TWO_PI 6.283185307179586476925286766559
#define DEG_TO_RAD 0.017453292519943295769236907684886
#define RAD_TO_DEG 57.295779513082320876798154814105
#define EULER 2.718281828459045235360287471352

#define SIM_PINS 256

unsigned long millis();
unsigned long micros();
void delay(unsigned long ms);
void delayMicroseconds(unsigned int us);
void yield();
void pinMode(int pin, int mode);
void digitalWrite(int pin, int value);
int digitalRead(int pin);
int analogRead(int pin);
void analogWrite(int pin, int value);

class SimSerial {
 public:
  void begin(unsigned long) {}
  int available();
  int read();
  void flush();
  size_t write(char c);
  int availableForWrite() { return 4096; }  // stdout never runs full
  size_t print(const char* s);
  size_t print(char c);
  size_t print(int v);
  size_t print(unsigned int v);
  size_t print(long v);
  size_t print(unsigned long v);
  size_t print(double v, int digits = 2);
  size_t println();
  template <typename X>
  size_t println(X v) { size_t n = print(v); return n + println(); }
  size_t println(double v, int digits) { size_t n = print(v, digits); return n + println(); }
  operator bool() const { return true; }
};

extern SimSerial Serial;
