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
