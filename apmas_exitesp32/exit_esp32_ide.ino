#include <ESP32Servo.h>

#define SERVO_PIN 18  // เปลี่ยนจาก 19

Servo exitServo;

const int OPEN_ANGLE  = 90;
const int CLOSE_ANGLE = 270;

bool isOpen = false;

void moveServo(int angle) {
  if (!exitServo.attached()) {
    Serial.println("!! Servo หลุด attach — กำลัง re-attach...");
    exitServo.attach(SERVO_PIN, 500, 2400);
    delay(100);
  }
  exitServo.write(angle);
}

void setup() {
  Serial.begin(115200);

  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  ESP32PWM::allocateTimer(2);
  ESP32PWM::allocateTimer(3);

  exitServo.setPeriodHertz(50);
  exitServo.attach(SERVO_PIN, 500, 2400);

  moveServo(CLOSE_ANGLE);
  delay(500);

  Serial.println("=== ระบบควบคุมทางออก ===");
  Serial.println("กด 'o' เพื่อเปิด | 'c' เพื่อปิด");
  printStatus();
}

void loop() {
  if (Serial.available() > 0) {
    char key = Serial.read();
    switch (key) {
      case 'o': case 'O': openDoor();  break;
      case 'c': case 'C': closeDoor(); break;
    }
  }
}

void openDoor() {
  if (!isOpen) {
    Serial.println(">> กำลังเปิด...");
    moveServo(OPEN_ANGLE);
    isOpen = true;
    delay(500);
    Serial.println(">> เปิดแล้ว ✓");
  } else {
    Serial.println(">> เปิดอยู่แล้ว");
  }
  printStatus();
}

void closeDoor() {
  if (isOpen) {
    Serial.println(">> กำลังปิด...");
    moveServo(CLOSE_ANGLE);
    isOpen = false;
    delay(500);
    Serial.println(">> ปิดแล้ว ✓");
  } else {
    Serial.println(">> ปิดอยู่แล้ว");
  }
  printStatus();
}

void printStatus() {
  Serial.print("สถานะ: ");
  Serial.println(isOpen ? "[ เปิด ]" : "[ ปิด ]");
}
