// FBD Studio: MQTT client for the MQTT blocks (ESP32, PubSubClient library).
//
// The network work (connecting, reconnecting, publishing, receiving) runs in its
// own FreeRTOS task, so a slow or unreachable broker never stalls the PLC scan
// cycle. Blocks exchange values with that task through a small table of topic
// slots protected by a mutex. The task never writes to Serial (that would mix
// with the monitor protocol); MQTT_CLIENT reports state changes from the scan.
#pragma once
#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include "plc_text.h"

namespace plc_mqtt {

static const int MAX_TOPICS = 32;
static const int VAL_LEN = 32;
static const int TOPIC_LEN = 128;

struct Slot {
  const char* topic;   // as entered on the block
  bool sub;            // true: subscription, false: publication
  bool retain;
  bool dirty;          // publication waiting to be sent
  bool fresh;          // received value not yet taken by the block
  char val[VAL_LEN];
};

struct Ctx {
  Slot slots[MAX_TOPICS];
  int n = 0;
  const char* host = "";
  uint16_t port = 1883;
  const char* user = "";
  const char* pass = "";
  const char* prefix = "";
  char id[40];
  char status_topic[TOPIC_LEN];
  volatile bool connected = false;
  volatile int state = -1;  // PubSubClient state(): 0 = connected, <0 network errors, >0 refused
  SemaphoreHandle_t lock = nullptr;
  TaskHandle_t task = nullptr;
  WiFiClient net;
  PubSubClient client;
  Ctx() : client(net) { id[0] = 0; status_topic[0] = 0; }
};
inline Ctx& ctx() { static Ctx c; return c; }

struct Guard {
  Guard() { xSemaphoreTake(ctx().lock, portMAX_DELAY); }
  ~Guard() { xSemaphoreGive(ctx().lock); }
};

inline void ensure_lock() {
  if (!ctx().lock) ctx().lock = xSemaphoreCreateMutex();
}

// "temp" with prefix "plant/line1" -> "plant/line1/temp"; "/abs/topic" -> "abs/topic"
inline void full_topic(const char* topic, char* out, size_t n) {
  const char* prefix = ctx().prefix;
  if (topic[0] == '/') snprintf(out, n, "%s", topic + 1);
  else if (prefix && prefix[0]) snprintf(out, n, "%s/%s", prefix, topic);
  else snprintf(out, n, "%s", topic);
}

// Called from the blocks' init(). Returns the slot index, or -1 if the table is full.
inline int add(const char* topic, bool sub, bool retain) {
  ensure_lock();
  Guard g;
  Ctx& c = ctx();
  if (c.n >= MAX_TOPICS) return -1;
  Slot& s = c.slots[c.n];
  s.topic = topic; s.sub = sub; s.retain = retain; s.dirty = false; s.fresh = false; s.val[0] = 0;
  return c.n++;
}

// Publication: hand a new value to the network task.
inline void set(int idx, const char* text) {
  if (idx < 0) return;
  Guard g;
  Slot& s = ctx().slots[idx];
  snprintf(s.val, VAL_LEN, "%s", text);
  s.dirty = true;
}

// Subscription: take a newly received value (returns false if nothing new).
inline bool take(int idx, char* out, size_t n) {
  if (idx < 0) return false;
  Guard g;
  Slot& s = ctx().slots[idx];
  if (!s.fresh) return false;
  snprintf(out, n, "%s", s.val);
  s.fresh = false;
  return true;
}

inline bool connected() { return ctx().connected; }
inline int state() { return ctx().state; }

inline void on_message(char* topic, byte* payload, unsigned int len) {
  Ctx& c = ctx();
  char full[TOPIC_LEN];
  Guard g;
  for (int i = 0; i < c.n; i++) {
    Slot& s = c.slots[i];
    if (!s.sub) continue;
    full_topic(s.topic, full, sizeof full);
    if (strcmp(full, topic) != 0) continue;
    unsigned int m = len < (unsigned)VAL_LEN - 1 ? len : VAL_LEN - 1;
    memcpy(s.val, payload, m);
    s.val[m] = 0;
    s.fresh = true;
  }
}

inline void task_main(void*) {
  Ctx& c = ctx();
  c.client.setServer(c.host, c.port);
  c.client.setCallback(on_message);
  c.client.setSocketTimeout(3);
  c.client.setKeepAlive(15);
  uint32_t last_try = 0;
  bool tried = false;
  int subscribed = 0;
  char full[TOPIC_LEN], val[VAL_LEN];
  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      c.connected = false;
      vTaskDelay(pdMS_TO_TICKS(250));
      continue;
    }
    if (!c.client.connected()) {
      c.connected = false;
      if (!tried || millis() - last_try > 5000) {
        tried = true;
        last_try = millis();
        const char* user = c.user[0] ? c.user : nullptr;
        const char* pass = c.user[0] ? c.pass : nullptr;
        if (c.client.connect(c.id, user, pass, c.status_topic, 0, true, "offline")) {
          c.client.publish(c.status_topic, "online", true);
          subscribed = 0;
          Guard g;  // after a reconnect, send every current value again
          for (int i = 0; i < c.n; i++) if (!c.slots[i].sub && c.slots[i].val[0]) c.slots[i].dirty = true;
        }
        c.state = c.client.state();
      }
      vTaskDelay(pdMS_TO_TICKS(100));
      continue;
    }
    c.state = 0;
    c.connected = true;
    for (; subscribed < c.n; subscribed++) {
      if (!c.slots[subscribed].sub) continue;
      full_topic(c.slots[subscribed].topic, full, sizeof full);
      c.client.subscribe(full);
    }
    for (int i = 0; i < c.n; i++) {
      bool send = false, retain = false;
      {
        Guard g;
        Slot& s = c.slots[i];
        if (!s.sub && s.dirty) {
          s.dirty = false; send = true; retain = s.retain;
          full_topic(s.topic, full, sizeof full);
          snprintf(val, sizeof val, "%s", s.val);
        }
      }
      if (send && !c.client.publish(full, val, retain)) {
        Guard g;
        c.slots[i].dirty = true;  // try again next round
      }
    }
    c.client.loop();
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}

// Called by MQTT_CLIENT's init(). The strings must stay valid (they are literals).
inline void start(const char* host, int port, const char* client_id, const char* user, const char* pass,
                  const char* prefix) {
  ensure_lock();
  Ctx& c = ctx();
  c.host = host; c.port = (uint16_t)port; c.user = user; c.pass = pass; c.prefix = prefix;
  if (client_id && client_id[0]) snprintf(c.id, sizeof c.id, "%s", client_id);
  else snprintf(c.id, sizeof c.id, "fbd-%06lx", (unsigned long)(ESP.getEfuseMac() & 0xFFFFFF));
  full_topic("status", c.status_topic, sizeof c.status_topic);
  if (c.task) return;
#if CONFIG_FREERTOS_UNICORE
  xTaskCreate(task_main, "plc_mqtt", 6144, nullptr, 1, &c.task);
#else
  // the scan cycle runs on core 1 (Arduino loop); the network task runs on core 0 with WiFi
  xTaskCreatePinnedToCore(task_main, "plc_mqtt", 6144, nullptr, 1, &c.task, 0);
#endif
}

}  // namespace plc_mqtt
