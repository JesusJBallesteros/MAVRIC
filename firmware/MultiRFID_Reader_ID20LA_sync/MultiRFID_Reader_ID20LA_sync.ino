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
  <timestamp_us>,SYNC,0   sync pulse rising edge (LED on SYNC_PIN turns on)
  <timestamp_us>,BOOT,0   once at start-up, marks a board reset

Sync pulse: SYNC_PIN goes HIGH for SYNC_PULSE_US at random intervals in
[SYNC_MIN_GAP_US, SYNC_MAX_GAP_US]. Wire an LED (with series resistor) that
every camera can see, and optionally the same pin to a camera GPIO input.
The random intervals make the pulse train self-identifying, so video and RFID
can be aligned even when pulses are missed.

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
const uint8_t       SYNC_PIN         = 52;         // free pin at the GND end of the MEGA's double header (was 22)
const unsigned long SYNC_PULSE_US    = 80000UL;    // LED on-time, >= 2 frame intervals: 80 ms ok down to 25 fps;
                                                   // e.g. 160000 for a 13 fps camera (keep rig.yaml sync.led_pulse_s equal)
const unsigned long SYNC_MIN_GAP_US  = 2000000UL;  // rising edge to rising edge, min
const unsigned long SYNC_MAX_GAP_US  = 6000000UL;  // and max
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

Stream* readerStream(int idx) {
  switch (idx) {
    case 0: return &Serial1;
    case 1: return &Serial2;
    case 2: return &Serial3;
    case 3: return &softReader;
    default: return nullptr;
  }
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

  pinMode(SYNC_PIN, OUTPUT);
  digitalWrite(SYNC_PIN, LOW);
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
    digitalWrite(SYNC_PIN, LOW);
    syncHigh = false;
  }
  if (!syncHigh && (nowUs - syncEdgeUs) >= syncGapUs) {
    digitalWrite(SYNC_PIN, HIGH);
    syncHigh = true;
    syncEdgeUs = nowUs;
    syncGapUs = nextSyncGap();
    logSample(nowUs, "SYNC", 0);
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

void logSample(unsigned long ts, const char* tag, int reader) {
  Serial.print(ts);
  Serial.print(',');
  Serial.print(tag);
  Serial.print(',');
  Serial.println(reader);
}
