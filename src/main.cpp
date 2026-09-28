#include <Adafruit_MAX31865.h>
#include <SPI.h>

// Use software SPI: CS, DI, DO, CLK
Adafruit_MAX31865 max = Adafruit_MAX31865(10, 11, 12, 13);

// The value of the Rref resistor. Use 430.0 for PT100 and 4300.0 for PT1000
#define RREF      430.0
// The 'nominal' 0-degrees-C resistance of the sensor
#define RNOMINAL  100.0

void setup() {
  Serial.begin(9600);
  max.begin(MAX31865_4WIRE);  // Set to 4WIRE or 2/3WIRE as needed
}

void loop() {
  if (Serial.available() > 0) {
    char command = Serial.read();
    if (command == 'r') {
      // Read temperature
      float temp = max.temperature(RNOMINAL, RREF);
      
      // Check for fault
      uint8_t fault = max.readFault();
      if (fault) {
        Serial.println("Fault detected!");
        max.clearFault();
      } else {
        Serial.println(temp);
      }
    }
  }
}