/* MAVRIC_IO.ino
MAVRIC output board: TTL outputs (Intan digital inputs, LED drivers, relays, ...)
switched by commands from the PC over USB serial. Runs on Uno, Nano, Nano Every,
MEGA or any Arduino-compatible board.

Serial: 115200 baud, ASCII lines terminated by '\n' ('\r' ignored).

Commands (<id> is a number chosen by the PC and echoed back):
  <id> P <pin> <ms>   pulse: pin HIGH now, LOW after <ms> milliseconds (non-blocking)
  <id> S <pin> <0|1>  set pin LOW/HIGH (cancels a running pulse on that pin)
  <id> T <pin>        toggle pin
  <id> A              all pins used so far LOW
  <id> W <ms>         watchdog: if no command arrives for <ms>, all LOW (0 = off)
  <id> H              heartbeat (keeps the watchdog happy, no reply)
  <id> ?              identify

Replies (<micros> = board micros() when the action happened):
  <micros>,ACK,<id>          command applied (pin already switched)
  <micros>,DONE,<id>         pulse ended
  <micros>,ERR,<id>,<reason> command rejected
  <micros>,INFO,MAVRIC_IO <version> pins=<n>
  <micros>,WDOG,0            watchdog fired, all outputs LOW
  <micros>,BOOT,0            board started

Pins 0 and 1 (USB serial) are refused.
*/

const char VERSION[] = "1.0";
const long BAUD = 115200;
const uint8_t MAX_PULSES = 16;
const uint8_t LINE_MAX = 48;

struct Pulse { uint8_t pin; unsigned long startMs; unsigned long durMs; long id; bool active; };
Pulse pulses[MAX_PULSES];

bool used[NUM_DIGITAL_PINS];
char line[LINE_MAX];
uint8_t lineLen = 0;
unsigned long watchdogMs = 0;
unsigned long lastCmdMs = 0;
bool watchdogTripped = false;

void reply(unsigned long us, const char* kind, long id) {
  Serial.print(us); Serial.print(','); Serial.print(kind); Serial.print(','); Serial.println(id);
}

void replyErr(long id, const char* reason) {
  Serial.print(micros()); Serial.print(",ERR,"); Serial.print(id); Serial.print(','); Serial.println(reason);
}

bool validPin(long pin) {
  return pin >= 2 && pin < NUM_DIGITAL_PINS;
}

void useOutput(uint8_t pin) {
  if (!used[pin]) {
    pinMode(pin, OUTPUT);
    used[pin] = true;
  }
}

void cancelPulse(uint8_t pin) {
  for (uint8_t i = 0; i < MAX_PULSES; ++i)
    if (pulses[i].active && pulses[i].pin == pin) pulses[i].active = false;
}

void allLow() {
  for (uint8_t i = 0; i < MAX_PULSES; ++i) pulses[i].active = false;
  for (uint8_t p = 0; p < NUM_DIGITAL_PINS; ++p)
    if (used[p]) digitalWrite(p, LOW);
}

void handle(char* s) {
  // tokens: id, command, up to two numbers
  char* tok[4] = {0, 0, 0, 0};
  uint8_t n = 0;
  for (char* t = strtok(s, " \t"); t && n < 4; t = strtok(NULL, " \t")) tok[n++] = t;
  if (n < 2) return;
  long id = atol(tok[0]);
  char cmd = tok[1][0];
  long a = n > 2 ? atol(tok[2]) : -1;
  long b = n > 3 ? atol(tok[3]) : -1;
  lastCmdMs = millis();
  watchdogTripped = false;

  switch (cmd) {
    case 'P': {
      if (!validPin(a) || b < 1) { replyErr(id, "bad pin or ms"); return; }
      uint8_t slot = MAX_PULSES;
      cancelPulse((uint8_t)a);
      for (uint8_t i = 0; i < MAX_PULSES; ++i) if (!pulses[i].active) { slot = i; break; }
      if (slot == MAX_PULSES) { replyErr(id, "too many pulses"); return; }
      useOutput((uint8_t)a);
      digitalWrite((uint8_t)a, HIGH);
      unsigned long us = micros();
      pulses[slot] = { (uint8_t)a, millis(), (unsigned long)b, id, true };
      reply(us, "ACK", id);
      return;
    }
    case 'S': {
      if (!validPin(a) || (b != 0 && b != 1)) { replyErr(id, "bad pin or value"); return; }
      cancelPulse((uint8_t)a);
      useOutput((uint8_t)a);
      digitalWrite((uint8_t)a, b ? HIGH : LOW);
      reply(micros(), "ACK", id);
      return;
    }
    case 'T': {
      if (!validPin(a)) { replyErr(id, "bad pin"); return; }
      cancelPulse((uint8_t)a);
      useOutput((uint8_t)a);
      digitalWrite((uint8_t)a, digitalRead((uint8_t)a) ? LOW : HIGH);
      reply(micros(), "ACK", id);
      return;
    }
    case 'A':
      allLow();
      reply(micros(), "ACK", id);
      return;
    case 'W':
      watchdogMs = a < 0 ? 0 : (unsigned long)a;
      reply(micros(), "ACK", id);
      return;
    case 'H':
      return;
    case '?':
      Serial.print(micros()); Serial.print(",INFO,MAVRIC_IO "); Serial.print(VERSION);
      Serial.print(" pins="); Serial.println(NUM_DIGITAL_PINS);
      return;
    default:
      replyErr(id, "unknown command");
  }
}

void setup() {
  Serial.begin(BAUD);
  for (uint8_t i = 0; i < MAX_PULSES; ++i) pulses[i].active = false;
  for (uint8_t p = 0; p < NUM_DIGITAL_PINS; ++p) used[p] = false;
  lastCmdMs = millis();
  reply(micros(), "BOOT", 0);
}

void loop() {
  // end pulses on time
  unsigned long nowMs = millis();
  for (uint8_t i = 0; i < MAX_PULSES; ++i) {
    if (pulses[i].active && nowMs - pulses[i].startMs >= pulses[i].durMs) {
      digitalWrite(pulses[i].pin, LOW);
      pulses[i].active = false;
      reply(micros(), "DONE", pulses[i].id);
    }
  }
  // watchdog: PC silent for too long -> everything off
  if (watchdogMs && !watchdogTripped && nowMs - lastCmdMs > watchdogMs) {
    allLow();
    watchdogTripped = true;
    reply(micros(), "WDOG", 0);
  }
  // commands
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      line[lineLen] = '\0';
      if (lineLen) handle(line);
      lineLen = 0;
    } else if (lineLen < LINE_MAX - 1) {
      line[lineLen++] = c;
    } else {
      lineLen = 0;  // overlong line: drop it
    }
  }
}
