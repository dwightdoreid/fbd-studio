// FBD Studio: Bluetooth Low Energy transport for the BT blocks (Nordic UART Service).
//
// The Nordic UART Service (NUS) is the de-facto "serial port over BLE". It is
// supported by iPhone and Android apps such as Bluefruit Connect, nRF Toolbox
// and "Serial Bluetooth Terminal", by Web Bluetooth and by PC tools.
//   service 6E400001-...  RX 6E400002-... (phone writes)  TX 6E400003-... (board notifies)
//
// With a passkey (100000..999999) the phone must pair with that 6-digit code
// before it can read or write anything; the pairing is remembered (bonding).
// The board counts as "connected" once the client has switched on notifications
// for TX (a phone connects first and subscribes a moment later; values sent in
// between would be lost). Received bytes arrive in the BLE task and are passed to the scan cycle
// through a small ring buffer guarded by a spinlock.
#pragma once
#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLESecurity.h>
#if defined(CONFIG_BLUEDROID_ENABLED)
#include <BLE2902.h>
#endif
#include "plc_bt.h"

namespace plc_ble {

static const char* const SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E";
static const char* const RX_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E";
static const char* const TX_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E";
static const size_t RX_SIZE = 512;
static const size_t CHUNK = 20;  // fits the smallest BLE packet (default MTU 23)

struct Ctx {
  BLEServer* server = nullptr;
  BLECharacteristic* tx = nullptr;
  volatile bool connected = false;
  volatile bool subscribed = false;  // NimBLE: set by onSubscribe
#if defined(CONFIG_BLUEDROID_ENABLED)
  BLE2902* cccd = nullptr;  // Bluedroid: notification switch of the TX characteristic
#endif
  portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
  uint8_t rx[RX_SIZE];
  volatile size_t head = 0, tail = 0;
};
inline Ctx& ctx() { static Ctx c; return c; }

struct ServerCb : public BLEServerCallbacks {
  void onConnect(BLEServer*) override { ctx().connected = true; }
  void onDisconnect(BLEServer* s) override {
    ctx().connected = false;
    ctx().subscribed = false;
    s->startAdvertising();  // be visible again for the next connection
  }
};

struct RxCb : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* ch) override {  // runs in the BLE task
    auto v = ch->getValue();
    Ctx& c = ctx();
    portENTER_CRITICAL(&c.mux);
    for (size_t i = 0; i < (size_t)v.length(); i++) {
      size_t next = (c.head + 1) % RX_SIZE;
      if (next == c.tail) break;  // full: drop the rest
      c.rx[c.head] = (uint8_t)v[i];
      c.head = next;
    }
    portEXIT_CRITICAL(&c.mux);
  }
};

#if !defined(CONFIG_BLUEDROID_ENABLED)
struct TxCb : public BLECharacteristicCallbacks {
  void onSubscribe(BLECharacteristic*, ble_gap_conn_desc*, uint16_t sub_value) override {
    ctx().subscribed = sub_value != 0;
  }
};
#endif

inline bool connected() {
  Ctx& c = ctx();
#if defined(CONFIG_BLUEDROID_ENABLED)
  return c.connected && c.cccd && c.cccd->getNotifications();
#else
  return c.connected && c.subscribed;
#endif
}

inline int read() {
  Ctx& c = ctx();
  int b = -1;
  portENTER_CRITICAL(&c.mux);
  if (c.tail != c.head) { b = c.rx[c.tail]; c.tail = (c.tail + 1) % RX_SIZE; }
  portEXIT_CRITICAL(&c.mux);
  return b;
}

inline void write(const char* data, size_t len) {
  Ctx& c = ctx();
  if (!connected() || !c.tx) return;
  for (size_t off = 0; off < len; off += CHUNK) {
    size_t n = len - off < CHUNK ? len - off : CHUNK;
    c.tx->setValue((const uint8_t*)data + off, n);
    c.tx->notify();
  }
}

// passkey: 0 = no pairing needed, 100000..999999 = 6-digit pairing code
inline void start(const char* name, uint32_t passkey) {
  static const plc_bt::Transport t = {connected, read, write};
  plc_bt::set_transport(&t);
  Ctx& c = ctx();
  if (c.server) return;
  BLEDevice::init(name);
  bool secure = passkey > 0;
  if (secure) {
    BLESecurity* sec = new BLESecurity();
    sec->setPassKey(true, passkey);
    sec->setCapability(ESP_IO_CAP_OUT);           // "display only": the phone asks for the code
    sec->setAuthenticationMode(true, true, true);  // bonding, MITM protection, secure connections
  }
  c.server = BLEDevice::createServer();
  c.server->setCallbacks(new ServerCb());
  BLEService* svc = c.server->createService(SERVICE_UUID);

  uint32_t tx_props = BLECharacteristic::PROPERTY_NOTIFY;
  uint32_t rx_props = BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR;
#if !defined(CONFIG_BLUEDROID_ENABLED)
  if (secure) {  // NimBLE (ESP32-S3/C3/...) secures characteristics through properties
    tx_props |= BLECharacteristic::PROPERTY_READ_AUTHEN;
    rx_props |= BLECharacteristic::PROPERTY_WRITE_AUTHEN;
  }
#endif
  c.tx = svc->createCharacteristic(TX_UUID, tx_props);
#if defined(CONFIG_BLUEDROID_ENABLED)
  c.cccd = new BLE2902();
  c.tx->addDescriptor(c.cccd);
#else
  c.tx->setCallbacks(new TxCb());  // NimBLE adds the notification descriptor itself
#endif
  BLECharacteristic* rx = svc->createCharacteristic(RX_UUID, rx_props);
  rx->setCallbacks(new RxCb());
#if defined(CONFIG_BLUEDROID_ENABLED)
  if (secure) {  // Bluedroid (original ESP32) secures characteristics through permissions
    c.tx->setAccessPermissions(ESP_GATT_PERM_READ_ENC_MITM | ESP_GATT_PERM_WRITE_ENC_MITM);
    rx->setAccessPermissions(ESP_GATT_PERM_WRITE_ENC_MITM);
    c.cccd->setAccessPermissions(ESP_GATT_PERM_READ_ENC_MITM | ESP_GATT_PERM_WRITE_ENC_MITM);
  }
#endif
  svc->start();
  BLEAdvertising* adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(SERVICE_UUID);
  adv->setScanResponse(true);
  adv->setMinPreferred(0x06);  // helps iPhones connect
  adv->setMaxPreferred(0x12);
  BLEDevice::startAdvertising();
}

}  // namespace plc_ble
