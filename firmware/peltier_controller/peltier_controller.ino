#include <Adafruit_MAX31865.h>
#include <DHT.h>
#include <SPI.h>

// Serial commands (9600 baud):
//   'r'  -> replies "pt100_temp,dht_humidity,dht_temp" (DHT values are "nan" if the DHT22 read fails)
//   "f<duty>" + newline -> H-bridge forward  (current M+ -> M-, PWM on RPWM), duty 0-255, e.g. "f128", replies "OK f128"
//   "b<duty>" + newline -> H-bridge backward (current M- -> M+, PWM on LPWM), duty 0-255, replies "OK b<duty>"
//   'o'  -> H-bridge off, replies "OK o"
// The BTS7960 needs >= ~7 V on B+, so the supply stays fixed and PWM (~980 Hz on pins 5/6) sets the power.

// MAX31865 configuration
Adafruit_MAX31865 max = Adafruit_MAX31865(10, 11, 12, 13);
#define RREF      430.0
#define RNOMINAL  100.0

// DHT22 configuration
#define DHTPIN 8     // DHT22 data pin
#define DHTTYPE DHT22
DHT dht(DHTPIN, DHTTYPE);

// BTS7960 H-bridge configuration
#define RPWM_PIN 5   // BTS7960 RPWM
#define LPWM_PIN 6   // BTS7960 LPWM
#define R_EN_PIN 4   // BTS7960 R_EN (optional 10k pull-down to GND)
#define L_EN_PIN 3   // BTS7960 L_EN (optional 10k pull-down to GND)
#define WATCHDOG_MS 5000  // Turn the bridge off if the PC sends nothing for this long

unsigned long lastCommandMs = 0;
char bridgeState = 'o';

void setBridge(char state, int duty) {
  if (state == bridgeState && state != 'o') {
    // Same direction: only change the PWM duty
    analogWrite(state == 'f' ? RPWM_PIN : LPWM_PIN, duty);
    return;
  }
  // Switch everything off first so both sides are never driven together
  digitalWrite(R_EN_PIN, LOW);
  digitalWrite(L_EN_PIN, LOW);
  digitalWrite(RPWM_PIN, LOW);
  digitalWrite(LPWM_PIN, LOW);
  if (state == 'f' || state == 'b') {
    delay(10);
    analogWrite(state == 'f' ? RPWM_PIN : LPWM_PIN, duty);
    digitalWrite(R_EN_PIN, HIGH);
    digitalWrite(L_EN_PIN, HIGH);
  }
  bridgeState = state;
}

void setup() {
  // Bridge off before anything else
  pinMode(R_EN_PIN, OUTPUT);
  pinMode(L_EN_PIN, OUTPUT);
  pinMode(RPWM_PIN, OUTPUT);
  pinMode(LPWM_PIN, OUTPUT);
  setBridge('o', 0);

  Serial.begin(9600);

  // Initialize MAX31865
  max.begin(MAX31865_4WIRE);  // Set to 4WIRE or 2/3WIRE as needed

  // Initialize DHT22
  dht.begin();
}

void loop() {
  if (bridgeState != 'o' && millis() - lastCommandMs > WATCHDOG_MS) {
    setBridge('o', 0);
  }

  if (Serial.available() > 0) {
    char command = Serial.read();
    lastCommandMs = millis();
    if (command == 'r') {
      // Offset for the Pt100 temperature (calculated from measurements)
      float offset = -2.34;
      // Read temperature from PT100
      float temperature = max.temperature(RNOMINAL, RREF) + offset;

      // Read humidity from DHT22
      float dht_humidity = dht.readHumidity();
      float dht_temperature = dht.readTemperature();

      // Check for faults
      uint8_t fault = max.readFault();
      if (fault) {
        Serial.println("Fault detected in MAX31865!");
        max.clearFault();
        return;
      }

      // Send temperature and humidity separated by comma
      Serial.print(temperature);
      Serial.print(",");
      Serial.print(dht_humidity);
      Serial.print(",");
      Serial.println(dht_temperature);
    } else if (command == 'f' || command == 'b') {
      int duty = Serial.parseInt();  // Read once: constrain() is a macro that would re-evaluate it
      duty = constrain(duty, 0, 255);
      setBridge(command, duty);
      Serial.print("OK ");
      Serial.print(command);
      Serial.println(duty);
    } else if (command == 'o') {
      setBridge('o', 0);
      Serial.println("OK o");
    }
  }
}
