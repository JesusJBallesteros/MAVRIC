/* MultiRFID_Reader_ID20LA_sync.ino
MAVRIC variant of MultiRFID_Reader_ID20LABoards.ino (MultiRFID, GPL-3.0).

Arduino MEGA, up to four ID-20LA readers (integrated antenna) read
simultaneously, one tag per reader:
  -Serial1, Serial2, Serial3 (hardware RX) for readers 1 to 3
  -SoftwareSerial for reader 4
  -Serial is the USB link to the PC at 500000 baud

Output lines (unchanged format, Multiple_RFID.m still parses them):
  <timestamp_us>,<TAGHEX>,<reader_number>   tag at reader in this round
  <timestamp_us>,NONE,0                      round without tags (heartbeat)
Added by this variant:
  <timestamp_us>,SYNC,0   sync pulse rising edge (the LED on SYNC_PINS turns on)
  <timestamp_us>,BOOT,0   once at start-up, marks a board reset
  <timestamp_us>,TRIGON,<period_ns>   frame trigger started: the time of its first pulse
  <timestamp_us>,TRIG,<n>             frame trigger pulse n (0, 1, 2, ...), about once a second
  <timestamp_us>,TRIGOFF,<n>          frame trigger stopped after n pulses
  <timestamp_us>,TRIGERR,0            a frame trigger command was refused
The TRIG times are the pulses' own times (taken in the timer interrupt), so
they can be a little older than the line printed before them.

Commands from the PC (one ASCII line each):
  F<fps>   frame trigger on TRIG_PIN at <fps> pulses per second (1-200, e.g. F30 or F29.97)
  F0       frame trigger off

Sync pulse: every pin in SYNC_PINS goes HIGH for SYNC_PULSE_US at random
intervals in [SYNC_MIN_GAP_US, SYNC_MAX_GAP_US], one load per pin:
  D52  the sync LED (with series resistor) that every camera can see
  D50  the audio divider into the MOTU M4 In 3
  D48  spare: Intan DIGITAL IN 2 or the camera GPIO input
The random intervals make the pulse train self-identifying, so video and RFID
can be aligned even when pulses are missed.

Frame trigger (for 3D: every camera exposes on the same pulse): Timer5 makes
a TRIG_PULSE_US pulse on TRIG_PIN (D46, OC5A) at the start of every frame, in
hardware, so the reader loop cannot delay it. The period is a whole number of
4 us timer ticks, from the same crystal as micros(). It runs from an F command
until F0 or a reset; the PC sends it when it connects (rig.yaml
sync: frame_trigger: source: rfid).

Author: Jesus J. Ballesteros (base), MAVRIC sync additions 2026-10
*/

#include <SoftwareSerial.h>

// == USER CONFIG ==
const int NUM_READERS = 4;  // set (1-4)

// TAG IN RANGE (TIR) pins, one per reader (HIGH while a tag is in range)
const uint8_t TAG_PINS[4] = {2, 3, 4, 5};

/* UART mapping on Mega (fixed by hardware):
Serial1 RX1 = pin 19; Reader 1
Serial2 RX2 = pin 17; Reader 2
Serial3 RX3 = pin 15; Reader 3
Reader 4 uses a software RX pin: */
const uint8_t SOFT_RX_PIN = 10;  // ID-20LA D0 -> MEGA D10
const uint8_t SOFT_TX_PIN = 11;  // unused

// TIMING
const unsigned long LOG_INTERVAL_US    = 125000UL; // sampling round, 125 ms = 8 rounds/s
const unsigned long TAG_CLEAR_DELAY_MS = 50;       // clear lastTag if TIR low > 50 ms

// SYNC PULSE
// Every pin in SYNC_PINS gives the same pulse; they switch within ~10 us of each other, less than one
// sample of the Intan (33 us) or the audio (21 us). One load per pin keeps each well under 20 mA.
const uint8_t       SYNC_PINS[]      = {52, 50, 48}; // D52: LED; D50: audio In 3; D48: spare (D22 before 2026-10-27)
const uint8_t       NUM_SYNC_PINS    = sizeof(SYNC_PINS) / sizeof(SYNC_PINS[0]);
const unsigned long SYNC_PULSE_US    = 80000UL;    // LED on-time, >= 2 frame intervals: 80 ms ok down to 25 fps;
                                                   // e.g. 160000 for a 13 fps camera (keep rig.yaml sync.led_pulse_s equal)
const unsigned long SYNC_MIN_GAP_US  = 2000000UL;  // rising edge to rising edge, min
const unsigned long SYNC_MAX_GAP_US  = 6000000UL;  // and max

// FRAME TRIGGER
const uint8_t       TRIG_PIN         = 46;         // OC5A: Timer5 drives it; to every camera's trigger input
const unsigned long TRIG_PULSE_US    = 1000UL;     // HIGH time of each pulse (cameras start on the rising edge)
const float         TRIG_MIN_FPS     = 1.0;
const float         TRIG_MAX_FPS     = 200.0;
// == END USER CONFIG ==

// === STATE ===
SoftwareSerial softReader(SOFT_RX_PIN, SOFT_TX_PIN);
String frameBufs[NUM_READERS];
unsigned long lastHighMillis[NUM_READERS] = {0};
unsigned long lastLogTimeUs = 0;

const int TAG_ID_MAX = 32;
char lastTag[NUM_READERS][TAG_ID_MAX];

bool syncHigh = false;
unsigned long syncEdgeUs = 0;   // time of the last rising edge
unsigned long syncGapUs = 0;    // time from syncEdgeUs to the next rising edge

char cmdBuf[16];                // a command from the PC
uint8_t cmdLen = 0;
bool trigRunning = false;
volatile unsigned int trigTicks = 0;      // period in 4 us ticks
unsigned long trigPeriodNs = 0;
volatile unsigned long trigEdges = 0;     // pulses so far
volatile unsigned long trigLogEvery = 1;  // a TRIG line every this many pulses (about once a second)
volatile bool trigPending = false;        // a pulse time waiting to be printed
volatile unsigned long trigPendUs = 0;
volatile unsigned long trigPendN = 0;

Stream* readerStream(int idx) {
  switch (idx) {
    case 0: return &Serial1;
    case 1: return &Serial2;
    case 2: return &Serial3;
    case 3: return &softReader;
    default: return nullptr;
  }
}

void setSync(uint8_t level) {
  for (uint8_t i = 0; i < NUM_SYNC_PINS; ++i) digitalWrite(SYNC_PINS[i], level);
}

unsigned long nextSyncGap() {
  return SYNC_MIN_GAP_US + (unsigned long)random((long)(SYNC_MAX_GAP_US - SYNC_MIN_GAP_US));
}

void setup() {
  Serial.begin(500000);

  Serial1.begin(9600);
  Serial2.begin(9600);
  Serial3.begin(9600);
  if (NUM_READERS >= 4) {
    softReader.begin(9600);
  }

  for (int i = 0; i < NUM_READERS; ++i) {
    pinMode(TAG_PINS[i], INPUT);
    lastHighMillis[i] = millis();
    lastTag[i][0] = '\0';
    frameBufs[i] = "";
  }

  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  for (uint8_t i = 0; i < NUM_SYNC_PINS; ++i) pinMode(SYNC_PINS[i], OUTPUT);
  setSync(LOW);
  pinMode(TRIG_PIN, OUTPUT);
  digitalWrite(TRIG_PIN, LOW);
  randomSeed(analogRead(A0) ^ (micros() << 8));  // floating A0 as entropy

  unsigned long now = micros();
  logSample(now, "BOOT", 0);
  lastLogTimeUs = now;
  syncEdgeUs = now;
  syncGapUs = nextSyncGap();
}

void loop() {
  unsigned long nowUs = micros();
  unsigned long nowMs = millis();
  bool anyTagFound = false;

  // Sync pulse (wrap-safe unsigned arithmetic)
  if (syncHigh && (nowUs - syncEdgeUs) >= SYNC_PULSE_US) {
    setSync(LOW);
    syncHigh = false;
  }
  if (!syncHigh && (nowUs - syncEdgeUs) >= syncGapUs) {
    setSync(HIGH);
    syncHigh = true;
    syncEdgeUs = nowUs;
    syncGapUs = nextSyncGap();
    logSample(nowUs, "SYNC", 0);
  }

  // Commands from the PC
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmdLen) {
        cmdBuf[cmdLen] = '\0';
        handleCommand(cmdBuf);
        cmdLen = 0;
      }
    } else if (cmdLen < sizeof(cmdBuf) - 1) {
      cmdBuf[cmdLen++] = c;
    }
  }

  // Frame trigger: print the pulse time the interrupt kept
  if (trigPending) {
    noInterrupts();
    unsigned long us = trigPendUs, n = trigPendN;
    trigPending = false;
    interrupts();
    if (n == 0) logValue(us, "TRIGON", trigPeriodNs);
    else logValue(us, "TRIG", n);
  }

  // Read incoming bytes from readers with TIR high
  for (int r = 0; r < NUM_READERS; ++r) {
    Stream* s = readerStream(r);
    if (!s) continue;

    if (digitalRead(TAG_PINS[r]) == HIGH) {
      lastHighMillis[r] = nowMs;
      while (s->available()) {
        char c = (char)s->read();
        frameBufs[r].concat(c);
        if (frameBufs[r].length() > 128) frameBufs[r].remove(0, frameBufs[r].length() - 128);
      }
      parseFramesInBuffer(r);
    } else if (nowMs - lastHighMillis[r] > TAG_CLEAR_DELAY_MS) {
      lastTag[r][0] = '\0';
    }
  }

  // Sampling round: one line per reader holding a tag, or NONE
  if ((nowUs - lastLogTimeUs) >= LOG_INTERVAL_US) {
    // fixed cadence; resynchronise if the loop fell more than one round behind
    lastLogTimeUs += LOG_INTERVAL_US;
    if ((nowUs - lastLogTimeUs) >= LOG_INTERVAL_US) lastLogTimeUs = nowUs;

    for (int r = 0; r < NUM_READERS; ++r) {
      if (digitalRead(TAG_PINS[r]) == HIGH && lastTag[r][0] != '\0') {
        logSample(nowUs, lastTag[r], r + 1);
        anyTagFound = true;
      }
    }
    if (!anyTagFound) {
      logSample(nowUs, "NONE", 0);
    }
    digitalWrite(LED_BUILTIN, anyTagFound ? HIGH : LOW);
  }
}

// Parse STX..ETX frames in frameBufs[idx] into lastTag[idx]
void parseFramesInBuffer(int idx) {
  String &buf = frameBufs[idx];
  int stxPos = buf.indexOf((char)0x02);
  while (stxPos >= 0) {
    int etxPos = buf.indexOf((char)0x03, stxPos + 1);
    if (etxPos < 0) break;
    String payload = buf.substring(stxPos + 1, etxPos);
    String clean = "";
    for (unsigned int i = 0; i < payload.length(); ++i) {
      char c = payload[i];
      if (c >= 32 && c <= 126) clean.concat(c);
    }
    int copyLen = min((int)clean.length(), TAG_ID_MAX - 1);
    for (int k = 0; k < copyLen; ++k) lastTag[idx][k] = clean[k];
    lastTag[idx][copyLen] = '\0';
    buf.remove(0, etxPos + 1);
    stxPos = buf.indexOf((char)0x02);
  }
}

void handleCommand(const char* cmd) {
  if (cmd[0] != 'F' && cmd[0] != 'f') {
    logValue(micros(), "TRIGERR", 0);
    return;
  }
  float fps = atof(cmd + 1);
  if (fps <= 0) {
    stopTrigger();
  } else if (fps >= TRIG_MIN_FPS && fps <= TRIG_MAX_FPS) {
    startTrigger(fps);
  } else {
    logValue(micros(), "TRIGERR", 0);
  }
}

// Timer5, fast PWM with TOP = ICR5 (mode 14), clock / 64 = 4 us ticks: OC5A (D46) goes HIGH at BOTTOM, the start
// of each period, and LOW after the pulse (OCR5A).
void startTrigger(float fps) {
  stopTrigger();
  unsigned long ticks = (unsigned long)(250000.0 / fps + 0.5);
  unsigned long pulse = min(TRIG_PULSE_US / 4, ticks / 2);
  noInterrupts();
  TCCR5A = 0;
  TCCR5B = 0;
  trigTicks = (unsigned int)ticks;
  trigPeriodNs = ticks * 4000UL;
  trigEdges = 0;
  trigLogEvery = max(1UL, (unsigned long)(fps + 0.5));
  trigPending = false;
  ICR5 = (unsigned int)(ticks - 1);
  OCR5A = (unsigned int)(pulse - 1);
  TCNT5 = (unsigned int)(ticks - 2);   // the first pulse two ticks from now
  TIFR5 = _BV(TOV5);                   // no stale overflow
  TIMSK5 = _BV(TOIE5);                 // count the pulses
  TCCR5A = _BV(COM5A1) | _BV(WGM51);
  TCCR5B = _BV(WGM53) | _BV(WGM52) | _BV(CS51) | _BV(CS50);  // starts it
  interrupts();
  trigRunning = true;
}

void stopTrigger() {
  if (!trigRunning) return;
  noInterrupts();
  TIMSK5 = 0;
  TCCR5B = 0;
  TCCR5A = 0;  // releases D46 to digitalWrite
  unsigned long n = trigEdges;
  interrupts();
  digitalWrite(TRIG_PIN, LOW);
  trigRunning = false;
  logValue(micros(), "TRIGOFF", n);
}

// At TOP: the counter wraps to BOTTOM, where D46 goes HIGH. The pulse time is micros() less the ticks since then,
// so a late interrupt (e.g. behind SoftwareSerial) does not shift it.
ISR(TIMER5_OVF_vect) {
  unsigned int c = TCNT5;
  unsigned long us = micros();
  long since = (c > trigTicks / 2) ? (long)c - (long)trigTicks : (long)c;  // negative: the edge is still to come
  unsigned long n = trigEdges++;
  if (n % trigLogEvery == 0) {
    trigPendUs = us - since * 4;
    trigPendN = n;
    trigPending = true;
  }
}

void logValue(unsigned long ts, const char* kind, unsigned long value) {
  Serial.print(ts);
  Serial.print(',');
  Serial.print(kind);
  Serial.print(',');
  Serial.println(value);
}

void logSample(unsigned long ts, const char* tag, int reader) {
  Serial.print(ts);
  Serial.print(',');
  Serial.print(tag);
  Serial.print(',');
  Serial.println(reader);
}
