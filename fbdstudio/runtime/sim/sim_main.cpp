// FBD Studio simulator host: emulates pins, time and the serial port.
//
// stdin lines are forwarded to the program's Serial input, except:
//   D <pin> <0|1>   set a simulated digital input
//   A <pin> <value> set a simulated analog input
// The process exits when stdin is closed (i.e. when the IDE goes away).
#include "Arduino.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <deque>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

void setup();
void loop();

SimSerial Serial;

static std::atomic<int> g_din[SIM_PINS];
static std::atomic<bool> g_din_set[SIM_PINS];
static std::atomic<int> g_ain[SIM_PINS];
static std::atomic<int> g_mode[SIM_PINS];
static std::atomic<int> g_out[SIM_PINS];
static std::mutex g_rx_mutex;
static std::deque<char> g_rx;
static const auto g_t0 = std::chrono::steady_clock::now();

static bool valid(int pin) { return pin >= 0 && pin < SIM_PINS; }

unsigned long millis() {
  return (unsigned long)std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::steady_clock::now() - g_t0).count();
}
unsigned long micros() {
  return (unsigned long)std::chrono::duration_cast<std::chrono::microseconds>(
             std::chrono::steady_clock::now() - g_t0).count();
}
void delay(unsigned long ms) { std::this_thread::sleep_for(std::chrono::milliseconds(ms)); }
void delayMicroseconds(unsigned int us) { std::this_thread::sleep_for(std::chrono::microseconds(us)); }
void yield() { std::this_thread::sleep_for(std::chrono::microseconds(100)); }

void pinMode(int pin, int mode) { if (valid(pin)) g_mode[pin] = mode; }
void digitalWrite(int pin, int value) { if (valid(pin)) g_out[pin] = value ? HIGH : LOW; }
int digitalRead(int pin) {
  if (!valid(pin)) return LOW;
  if (g_din_set[pin]) return g_din[pin];
  return g_mode[pin] == INPUT_PULLUP ? HIGH : LOW;
}
int analogRead(int pin) { return valid(pin) ? g_ain[pin].load() : 0; }
void analogWrite(int pin, int value) { if (valid(pin)) g_out[pin] = value; }

// ---------------------------------------------------------------- serial
int SimSerial::available() {
  std::lock_guard<std::mutex> lock(g_rx_mutex);
  return (int)g_rx.size();
}
int SimSerial::read() {
  std::lock_guard<std::mutex> lock(g_rx_mutex);
  if (g_rx.empty()) return -1;
  char c = g_rx.front();
  g_rx.pop_front();
  return (unsigned char)c;
}
void SimSerial::flush() { fflush(stdout); }
size_t SimSerial::write(char c) { return fputc(c, stdout) == EOF ? 0 : 1; }
size_t SimSerial::print(const char* s) { return (size_t)printf("%s", s); }
size_t SimSerial::print(char c) { return write(c); }
size_t SimSerial::print(int v) { return (size_t)printf("%d", v); }
size_t SimSerial::print(unsigned int v) { return (size_t)printf("%u", v); }
size_t SimSerial::print(long v) { return (size_t)printf("%ld", v); }
size_t SimSerial::print(unsigned long v) { return (size_t)printf("%lu", v); }
size_t SimSerial::print(double v, int digits) {
  if (isnan(v)) return print("nan");
  if (isinf(v)) return print("inf");
  return (size_t)printf("%.*f", digits, v);
}
size_t SimSerial::println() {
  size_t n = (size_t)printf("\r\n");
  fflush(stdout);
  return n;
}

static void stdin_reader() {
  std::string line;
  while (std::getline(std::cin, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    int pin = 0, value = 0;
    if (line.size() > 2 && (line[0] == 'D' || line[0] == 'A') && line[1] == ' ' &&
        sscanf(line.c_str() + 2, "%d %d", &pin, &value) == 2) {
      if (valid(pin)) {
        if (line[0] == 'D') { g_din[pin] = value ? HIGH : LOW; g_din_set[pin] = true; }
        else g_ain[pin] = value;
      }
      continue;
    }
    std::lock_guard<std::mutex> lock(g_rx_mutex);
    for (char c : line) g_rx.push_back(c);
    g_rx.push_back('\n');
  }
  fflush(stdout);
  std::_Exit(0);  // IDE closed the pipe
}

int main() {
#ifdef _WIN32
  _setmode(_fileno(stdout), _O_BINARY);  // keep "\r\n" as is (text mode would emit "\r\r\n")
#endif
  for (int i = 0; i < SIM_PINS; i++) {
    g_din[i] = 0; g_din_set[i] = false; g_ain[i] = 0; g_mode[i] = INPUT; g_out[i] = 0;
  }
  std::thread(stdin_reader).detach();
  setup();
  for (;;) {
    loop();
    std::this_thread::sleep_for(std::chrono::microseconds(200));
  }
}
