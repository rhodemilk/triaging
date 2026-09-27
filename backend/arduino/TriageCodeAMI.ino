#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <MFRC522.h>
#include <ESP_I2S.h>
#include <SD_MMC.h>
#include "esp_camera.h"
#include "img_converters.h"

// ======================================================
// WIFI
// ======================================================

const char* WIFI_SSID = "Tiffany";
const char* WIFI_PASSWORD = "12345678";

// ======================================================
// BACKEND
// ======================================================

const char* SERVER_IP = "172.20.10.6";
const int SERVER_PORT = 5001;

const char* SESSION_ENDPOINT =
  "http://172.20.10.6:5001/api/triage/session";

// ======================================================
// RFID
// ======================================================

#define RFID_SS_PIN    5
#define RFID_RST_PIN   22
#define RFID_SCK_PIN   18
#define RFID_MISO_PIN  19
#define RFID_MOSI_PIN  23

MFRC522 rfid(RFID_SS_PIN, RFID_RST_PIN);

// ======================================================
// AUDIO
//
// CURRENT MIC + SPEAKER WIRING
//
// BCLK        -> GPIO 4
// WS / LRC    -> GPIO 13
// Speaker DIN -> GPIO 33
// Mic SD      -> GPIO 32
// ======================================================

#define AUDIO_BCLK       4
#define AUDIO_WS         13
#define SPEAKER_DOUT     33
#define MIC_DIN          32

#define MIC_SAMPLE_RATE  16000
#define RECORD_SECONDS   5

// No voice activity detection is used.
// Each deliberate wristband tap records exactly one 5-second turn.

I2SClass audio(I2S_NUM_1);

// ======================================================
// WROVER CAMERA PINS
//
// These are shared with RFID/audio.
// They are only used AFTER those peripherals are released.
// ======================================================

#define CAM_Y2      4
#define CAM_Y3      5
#define CAM_Y4      18
#define CAM_Y5      19
#define CAM_Y6      36
#define CAM_Y7      39
#define CAM_Y8      34
#define CAM_Y9      35

#define CAM_XCLK    21
#define CAM_PCLK    22
#define CAM_VSYNC   25
#define CAM_HREF    23

#define CAM_SIOD    26
#define CAM_SIOC    27

#define CAM_PWDN    -1
#define CAM_RESET   -1

// ======================================================
// PATIENT SESSION
// ======================================================

bool patientSessionActive = false;
bool rfidReleased = false;
bool cameraIsRunning = false;

// NEW:
// remembers whether the previous session ended because
// the 10-second pause timer expired
bool sessionTimedOut = false;

// NEW:
// 10-second paused conversation timer
unsigned long conversationPausedAt = 0;
const unsigned long CONVERSATION_TIMEOUT_MS = 10000;

String currentPatientUID = "";
String currentSessionID = "";
String currentVoiceEndpoint = "";

// ======================================================
// WAV INFO
// ======================================================

struct WavInfo {
  uint16_t format = 0;
  uint16_t channels = 0;
  uint32_t sampleRate = 0;
  uint16_t bitsPerSample = 0;
  uint32_t dataSize = 0;
};

// ======================================================
// LITTLE ENDIAN HELPERS
// ======================================================

uint16_t readLE16(
  const uint8_t* p
) {
  return
    (uint16_t)p[0] |
    ((uint16_t)p[1] << 8);
}

uint32_t readLE32(
  const uint8_t* p
) {
  return
    (uint32_t)p[0] |
    ((uint32_t)p[1] << 8) |
    ((uint32_t)p[2] << 16) |
    ((uint32_t)p[3] << 24);
}

// ======================================================
// STREAM HELPERS
// ======================================================

bool readExact(
  Stream& stream,
  uint8_t* buffer,
  size_t length
) {
  size_t received = 0;

  while (received < length) {

    int amount =
      stream.readBytes(
        (char*)(buffer + received),
        length - received
      );

    if (amount <= 0) {
      return false;
    }

    received += amount;
  }

  return true;
}

bool skipBytes(
  Stream& stream,
  uint32_t count
) {
  uint8_t temp[128];

  while (count > 0) {

    uint32_t amount =
      min(
        (uint32_t)sizeof(temp),
        count
      );

    if (!readExact(
          stream,
          temp,
          amount
        )) {
      return false;
    }

    count -= amount;
  }

  return true;
}

// ======================================================
// WIFI
// ======================================================

void connectWiFi() {

  Serial.println();

  Serial.print(
    "Connecting to "
  );

  Serial.println(
    WIFI_SSID
  );

  WiFi.mode(
    WIFI_STA
  );

  WiFi.begin(
    WIFI_SSID,
    WIFI_PASSWORD
  );

  while (
    WiFi.status() != WL_CONNECTED
  ) {

    delay(500);

    Serial.print(".");
  }

  Serial.println();

  Serial.println(
    "WiFi connected!"
  );

  Serial.print(
    "ESP32 IP: "
  );

  Serial.println(
    WiFi.localIP()
  );
}

// ======================================================
// PARSE WAV
// ======================================================

bool parseWav(
  Stream& stream,
  WavInfo& info
) {

  uint8_t riff[12];

  if (!readExact(
        stream,
        riff,
        12
      )) {

    Serial.println(
      "ERROR reading WAV header"
    );

    return false;
  }

  if (
    memcmp(riff, "RIFF", 4) != 0 ||
    memcmp(riff + 8, "WAVE", 4) != 0
  ) {

    Serial.println(
      "ERROR: Not a WAV file"
    );

    return false;
  }

  bool foundFmt = false;

  while (true) {

    uint8_t chunkHeader[8];

    if (!readExact(
          stream,
          chunkHeader,
          8
        )) {

      return false;
    }

    uint32_t chunkSize =
      readLE32(
        chunkHeader + 4
      );

    // ==================================================
    // FORMAT
    // ==================================================

    if (
      memcmp(
        chunkHeader,
        "fmt ",
        4
      ) == 0
    ) {

      uint8_t fmt[16];

      if (!readExact(
            stream,
            fmt,
            16
          )) {

        return false;
      }

      info.format =
        readLE16(fmt);

      info.channels =
        readLE16(
          fmt + 2
        );

      info.sampleRate =
        readLE32(
          fmt + 4
        );

      info.bitsPerSample =
        readLE16(
          fmt + 14
        );

      if (
        chunkSize > 16
      ) {

        if (!skipBytes(
              stream,
              chunkSize - 16
            )) {

          return false;
        }
      }

      foundFmt = true;
    }

    // ==================================================
    // AUDIO DATA
    // ==================================================

    else if (
      memcmp(
        chunkHeader,
        "data",
        4
      ) == 0
    ) {

      if (!foundFmt) {
        return false;
      }

      info.dataSize =
        chunkSize;

      return true;
    }

    else {

      if (!skipBytes(
            stream,
            chunkSize
          )) {

        return false;
      }
    }

    if (
      chunkSize & 1
    ) {

      uint8_t dummy;

      stream.readBytes(
        (char*)&dummy,
        1
      );
    }
  }
}

// ======================================================
// PLAY WAV FROM SD
// ======================================================

bool playWav(
  const char* filename
) {

  Serial.println();
  Serial.println("==============================");
  Serial.println("PLAYING AUDIO");
  Serial.println("==============================");

  Serial.print(
    "File: "
  );

  Serial.println(
    filename
  );

  File file =
    SD_MMC.open(
      filename,
      FILE_READ
    );

  if (!file) {

    Serial.println(
      "ERROR opening WAV"
    );

    return false;
  }

  WavInfo info;

  if (!parseWav(
        file,
        info
      )) {

    file.close();

    return false;
  }

  Serial.print(
    "Sample rate: "
  );

  Serial.println(
    info.sampleRate
  );

  Serial.print(
    "Channels: "
  );

  Serial.println(
    info.channels
  );

  Serial.print(
    "Bits: "
  );

  Serial.println(
    info.bitsPerSample
  );

  if (
    info.format != 1 ||
    info.bitsPerSample != 16
  ) {

    Serial.println(
      "ERROR: WAV must be 16-bit PCM"
    );

    file.close();

    return false;
  }

  // Camera must NOT be active here.

  if (cameraIsRunning) {

    Serial.println(
      "ERROR: Camera is still active"
    );

    file.close();

    return false;
  }

  audio.end();

  delay(100);

  audio.setPins(
    AUDIO_BCLK,
    AUDIO_WS,
    SPEAKER_DOUT,
    MIC_DIN
  );

  if (!audio.begin(
        I2S_MODE_STD,
        info.sampleRate,
        I2S_DATA_BIT_WIDTH_16BIT,
        I2S_SLOT_MODE_STEREO
      )) {

    Serial.println(
      "Speaker I2S failed"
    );

    file.close();

    return false;
  }

  uint8_t inputBuffer[512];
  int16_t stereoBuffer[512];

  uint32_t remaining =
    info.dataSize;

  while (
    remaining > 0
  ) {

    int amount =
      min(
        (uint32_t)sizeof(inputBuffer),
        remaining
      );

    int bytesRead =
      file.read(
        inputBuffer,
        amount
      );

    if (
      bytesRead <= 0
    ) {

      break;
    }

    remaining -=
      bytesRead;

    // ==================================================
    // MONO -> STEREO
    // ==================================================

    if (
      info.channels == 1
    ) {

      int samples =
        bytesRead / 2;

      for (
        int i = 0;
        i < samples;
        i++
      ) {

        int16_t sample =
          (int16_t)readLE16(
            &inputBuffer[
              i * 2
            ]
          );

        stereoBuffer[
          i * 2
        ] =
          sample;

        stereoBuffer[
          i * 2 + 1
        ] =
          sample;
      }

      audio.write(
        (uint8_t*)stereoBuffer,
        samples *
          2 *
          sizeof(int16_t)
      );
    }

    // ==================================================
    // ALREADY STEREO
    // ==================================================

    else {

      audio.write(
        inputBuffer,
        bytesRead
      );
    }
  }

  file.close();

  delay(300);

  audio.end();

  Serial.println(
    "AUDIO FINISHED"
  );

  return true;
}

// ======================================================
// RFID UID
// ======================================================

String getUIDString() {

  String uid = "";

  for (
    byte i = 0;
    i < rfid.uid.size;
    i++
  ) {

    if (
      rfid.uid.uidByte[i] <
      0x10
    ) {

      uid += "0";
    }

    uid += String(
      rfid.uid.uidByte[i],
      HEX
    );
  }

  uid.toUpperCase();

  return uid;
}

// ======================================================
// SIMPLE JSON STRING EXTRACTOR
// ======================================================

String extractJsonString(
  const String& text,
  const String& key
) {

  String search =
    "\"" +
    key +
    "\"";

  int keyPosition =
    text.indexOf(
      search
    );

  if (
    keyPosition == -1
  ) {

    return "";
  }

  int colon =
    text.indexOf(
      ':',
      keyPosition
    );

  if (
    colon == -1
  ) {

    return "";
  }

  int firstQuote =
    text.indexOf(
      '"',
      colon + 1
    );

  if (
    firstQuote == -1
  ) {

    return "";
  }

  int secondQuote =
    text.indexOf(
      '"',
      firstQuote + 1
    );

  if (
    secondQuote == -1
  ) {

    return "";
  }

  return text.substring(
    firstQuote + 1,
    secondQuote
  );
}

// ======================================================
// CREATE PATIENT SESSION
// ======================================================

bool createPatientSession(
  const String& uid
) {

  if (
    WiFi.status() !=
    WL_CONNECTED
  ) {

    connectWiFi();
  }

  HTTPClient http;

  http.setTimeout(
    30000
  );

  if (!http.begin(
        SESSION_ENDPOINT
      )) {

    Serial.println(
      "Could not start session POST"
    );

    return false;
  }

  http.addHeader(
    "Content-Type",
    "application/json"
  );

  String body =
    "{\"nfc_uid\":\"" +
    uid +
    "\"}";

  Serial.println();
  Serial.println("==============================");
  Serial.println("CREATING PATIENT SESSION");
  Serial.println("==============================");

  Serial.print(
    "POST BODY: "
  );

  Serial.println(
    body
  );

  int status =
    http.POST(
      body
    );

  String response =
    http.getString();

  http.end();

  Serial.print(
    "HTTP STATUS: "
  );

  Serial.println(
    status
  );

  Serial.println(
    "SERVER RESPONSE:"
  );

  Serial.println(
    response
  );

  if (
    status < 200 ||
    status >= 300
  ) {

    Serial.println(
      "SESSION CREATION FAILED"
    );

    return false;
  }

  currentSessionID =
    extractJsonString(
      response,
      "session_id"
    );

  if (
    currentSessionID.length() ==
    0
  ) {

    Serial.println(
      "session_id missing"
    );

    return false;
  }

  currentPatientUID =
    uid;

  patientSessionActive =
    true;

  currentVoiceEndpoint =
    "http://172.20.10.6:5001"
    "/api/triage/session/" +
    currentSessionID +
    "/voice";

  Serial.println();

  Serial.println(
    "PATIENT SESSION ACTIVE"
  );

  Serial.print(
    "Session ID: "
  );

  Serial.println(
    currentSessionID
  );

  return true;
}

// ======================================================
// RELEASE RFID PINS
// ======================================================

void shutdownRFIDForCamera() {

  if (
    rfidReleased
  ) {

    return;
  }

  Serial.println();

  Serial.println(
    "Releasing RFID pins for camera..."
  );

  rfid.PCD_AntennaOff();

  SPI.end();

  pinMode(
    RFID_SS_PIN,
    INPUT
  );

  pinMode(
    RFID_RST_PIN,
    INPUT
  );

  pinMode(
    RFID_SCK_PIN,
    INPUT
  );

  pinMode(
    RFID_MISO_PIN,
    INPUT
  );

  pinMode(
    RFID_MOSI_PIN,
    INPUT
  );

  rfidReleased =
    true;

  Serial.println(
    "RFID released"
  );
}

// ======================================================
// RESTORE RFID AFTER CAMERA
// ======================================================

void restoreRFID() {

  if (!rfidReleased) {
    return;
  }

  Serial.println();

  Serial.println(
    "Restoring RFID reader..."
  );

  SPI.begin(
    RFID_SCK_PIN,
    RFID_MISO_PIN,
    RFID_MOSI_PIN,
    RFID_SS_PIN
  );

  rfid.PCD_Init();

  delay(100);

  rfid.PCD_AntennaOn();

  rfidReleased = false;

  Serial.println(
    "RFID READY FOR NEXT TURN"
  );
}

// ======================================================
// END CURRENT PATIENT SESSION LOCALLY
// ======================================================

void endPatientSession() {

  audio.end();

  if (cameraIsRunning) {
    shutdownCamera();
  }

  if (rfidReleased) {
    restoreRFID();
  }

  patientSessionActive = false;

  currentPatientUID = "";
  currentSessionID = "";
  currentVoiceEndpoint = "";

  Serial.println();
  Serial.println("=================================");
  Serial.println("PATIENT SESSION ENDED");
  Serial.println("=================================");
  Serial.println("Tap a wristband to start a new session.");
}

// ======================================================
// INITIALIZE CAMERA
// ======================================================

bool initCamera() {

  Serial.println();
  Serial.println("==============================");
  Serial.println("INITIALIZING CAMERA");
  Serial.println("==============================");

  audio.end();

  delay(200);

  camera_config_t config = {};

  config.ledc_channel =
    LEDC_CHANNEL_0;

  config.ledc_timer =
    LEDC_TIMER_0;

  config.pin_d0 =
    CAM_Y2;

  config.pin_d1 =
    CAM_Y3;

  config.pin_d2 =
    CAM_Y4;

  config.pin_d3 =
    CAM_Y5;

  config.pin_d4 =
    CAM_Y6;

  config.pin_d5 =
    CAM_Y7;

  config.pin_d6 =
    CAM_Y8;

  config.pin_d7 =
    CAM_Y9;

  config.pin_xclk =
    CAM_XCLK;

  config.pin_pclk =
    CAM_PCLK;

  config.pin_vsync =
    CAM_VSYNC;

  config.pin_href =
    CAM_HREF;

  config.pin_sccb_sda =
    CAM_SIOD;

  config.pin_sccb_scl =
    CAM_SIOC;

  config.pin_pwdn =
    CAM_PWDN;

  config.pin_reset =
    CAM_RESET;

  config.xclk_freq_hz =
    20000000;

  config.pixel_format =
    PIXFORMAT_RGB565;

  config.frame_size =
    FRAMESIZE_240X240;

  config.grab_mode =
    CAMERA_GRAB_WHEN_EMPTY;

  config.fb_location =
    CAMERA_FB_IN_PSRAM;

  config.jpeg_quality =
    12;

  config.fb_count =
    1;

  esp_err_t err =
    esp_camera_init(
      &config
    );

  if (
    err != ESP_OK
  ) {

    Serial.printf(
      "CAMERA INIT FAILED: 0x%x\n",
      err
    );

    cameraIsRunning =
      false;

    return false;
  }

  sensor_t* sensor =
    esp_camera_sensor_get();

  if (
    sensor &&
    sensor->id.PID ==
      OV3660_PID
  ) {

    sensor->set_vflip(
      sensor,
      1
    );

    sensor->set_brightness(
      sensor,
      1
    );

    sensor->set_saturation(
      sensor,
      -2
    );
  }

  cameraIsRunning =
    true;

  Serial.println(
    "CAMERA READY - RGB565"
  );

  return true;
}

// ======================================================
// SHUT CAMERA DOWN
// ======================================================

void shutdownCamera() {

  if (
    !cameraIsRunning
  ) {

    return;
  }

  Serial.println();

  Serial.println(
    "Turning camera off..."
  );

  esp_camera_deinit();

  cameraIsRunning =
    false;

  delay(300);

  pinMode(
    CAM_Y2,
    INPUT
  );

  pinMode(
    CAM_Y3,
    INPUT
  );

  pinMode(
    CAM_Y4,
    INPUT
  );

  pinMode(
    CAM_Y5,
    INPUT
  );

  Serial.println(
    "Camera released"
  );
}

// ======================================================
// CAPTURE + SEND PATIENT IMAGE
// ======================================================

String captureAndSendPatientImage() {

  if (
    !cameraIsRunning
  ) {

    Serial.println(
      "Camera is not running"
    );

    return "";
  }

  Serial.println();
  Serial.println("==============================");
  Serial.println("PATIENT CAMERA REQUEST");
  Serial.println("==============================");

  Serial.println(
    "Hold injury in front of camera."
  );

  Serial.println(
    "Taking photo in 3 seconds..."
  );

  delay(3000);

  camera_fb_t* warmup =
    esp_camera_fb_get();

  if (
    warmup
  ) {

    esp_camera_fb_return(
      warmup
    );
  }

  delay(150);

  Serial.println(
    "Taking photo..."
  );

  camera_fb_t* frame =
    esp_camera_fb_get();

  if (
    !frame
  ) {

    Serial.println(
      "CAMERA CAPTURE FAILED"
    );

    return "";
  }

  Serial.println(
    "RGB565 PHOTO CAPTURED"
  );

  Serial.print(
    "Resolution: "
  );

  Serial.print(
    frame->width
  );

  Serial.print(
    " x "
  );

  Serial.println(
    frame->height
  );

  Serial.print(
    "Raw RGB565 bytes: "
  );

  Serial.println(
    frame->len
  );

  uint8_t* jpgBuffer =
    nullptr;

  size_t jpgLength =
    0;

  Serial.println(
    "Converting RGB565 to JPEG..."
  );

  bool converted =
    frame2jpg(
      frame,
      80,
      &jpgBuffer,
      &jpgLength
    );

  esp_camera_fb_return(
    frame
  );

  if (
    !converted ||
    jpgBuffer == nullptr ||
    jpgLength == 0
  ) {

    Serial.println(
      "RGB565 -> JPEG CONVERSION FAILED"
    );

    if (
      jpgBuffer != nullptr
    ) {

      free(
        jpgBuffer
      );
    }

    return "";
  }

  Serial.println(
    "JPEG CONVERSION SUCCESS"
  );

  Serial.print(
    "JPEG bytes: "
  );

  Serial.println(
    jpgLength
  );

  String path =
    "/api/triage/session/" +
    currentSessionID +
    "/image";

  String boundary =
    "----GREENROVERIMAGE";

  String head =
    "--" +
    boundary +
    "\r\n"
    "Content-Disposition: form-data; "
    "name=\"image\"; "
    "filename=\"patient.jpg\"\r\n"
    "Content-Type: image/jpeg\r\n"
    "\r\n";

  String tail =
    "\r\n--" +
    boundary +
    "--\r\n";

  size_t contentLength =
    head.length() +
    jpgLength +
    tail.length();

  Serial.println();
  Serial.println("==============================");
  Serial.println("POSTING PATIENT IMAGE");
  Serial.println("==============================");

  Serial.print(
    "POST "
  );

  Serial.println(
    path
  );

  WiFiClient client;

  if (!client.connect(
        SERVER_IP,
        SERVER_PORT
      )) {

    Serial.println(
      "IMAGE BACKEND CONNECTION FAILED"
    );

    free(
      jpgBuffer
    );

    return "";
  }

  Serial.println(
    "Connected to image backend"
  );

  client.print(
    "POST " +
    path +
    " HTTP/1.1\r\n"
  );

  client.print(
    "Host: " +
    String(SERVER_IP) +
    ":" +
    String(SERVER_PORT) +
    "\r\n"
  );

  client.print(
    "Content-Type: multipart/form-data; boundary=" +
    boundary +
    "\r\n"
  );

  client.print(
    "Content-Length: "
  );

  client.print(
    contentLength
  );

  client.print(
    "\r\n"
  );

  client.print(
    "Connection: close\r\n"
  );

  client.print(
    "\r\n"
  );

  client.print(
    head
  );

  size_t sent =
    0;

  const size_t CHUNK_SIZE =
    1024;

  while (
    sent <
    jpgLength
  ) {

    size_t amount =
      min(
        CHUNK_SIZE,
        jpgLength - sent
      );

    size_t written =
      client.write(
        jpgBuffer + sent,
        amount
      );

    if (
      written == 0
    ) {

      Serial.println(
        "IMAGE UPLOAD INTERRUPTED"
      );

      free(
        jpgBuffer
      );

      client.stop();

      return "";
    }

    sent +=
      written;
  }

  free(
    jpgBuffer
  );

  jpgBuffer =
    nullptr;

  client.print(
    tail
  );

  Serial.print(
    "Uploaded JPEG bytes: "
  );

  Serial.println(
    sent
  );

  Serial.println(
    "Waiting for Gemini image analysis + Amy response..."
  );

  unsigned long started =
    millis();

  while (
    client.connected() &&
    !client.available()
  ) {

    if (
      millis() -
      started >
      120000
    ) {

      Serial.println(
        "IMAGE API TIMEOUT"
      );

      client.stop();

      return "";
    }

    delay(10);
  }

  String response =
    "";

  while (
    client.connected() ||
    client.available()
  ) {

    while (
      client.available()
    ) {

      response +=
        (char)client.read();
    }

    delay(1);
  }

  client.stop();

  Serial.println();
  Serial.println("==============================");
  Serial.println("IMAGE ANALYSIS RESPONSE");
  Serial.println("==============================");

  Serial.println(
    response
  );

  bool success =
    (
      response.indexOf(
        "HTTP/1.1 200"
      ) >= 0
      ||
      response.indexOf(
        "HTTP/1.0 200"
      ) >= 0
    );

  if (
    !success
  ) {

    Serial.println(
      "IMAGE ANALYSIS FAILED"
    );

    return "";
  }

  Serial.println(
    "IMAGE ANALYSIS SUCCESS"
  );

  return response;
}

// ======================================================
// WRITE WAV HEADER
// ======================================================

void writeWavHeader(
  File& file,
  uint32_t dataSize
) {

  uint32_t sampleRate =
    MIC_SAMPLE_RATE;

  uint16_t channels =
    1;

  uint16_t bits =
    16;

  uint32_t byteRate =
    sampleRate *
    channels *
    bits /
    8;

  uint16_t blockAlign =
    channels *
    bits /
    8;

  uint32_t riffSize =
    36 +
    dataSize;

  file.seek(0);

  file.write(
    (const uint8_t*)"RIFF",
    4
  );

  file.write(
    (uint8_t*)&riffSize,
    4
  );

  file.write(
    (const uint8_t*)"WAVE",
    4
  );

  file.write(
    (const uint8_t*)"fmt ",
    4
  );

  uint32_t fmtSize =
    16;

  file.write(
    (uint8_t*)&fmtSize,
    4
  );

  uint16_t pcm =
    1;

  file.write(
    (uint8_t*)&pcm,
    2
  );

  file.write(
    (uint8_t*)&channels,
    2
  );

  file.write(
    (uint8_t*)&sampleRate,
    4
  );

  file.write(
    (uint8_t*)&byteRate,
    4
  );

  file.write(
    (uint8_t*)&blockAlign,
    2
  );

  file.write(
    (uint8_t*)&bits,
    2
  );

  file.write(
    (const uint8_t*)"data",
    4
  );

  file.write(
    (uint8_t*)&dataSize,
    4
  );
}

// ======================================================
// RECORD PATIENT AUDIO
// ======================================================

bool recordPatientAudio(
  const char* filename
) {

  Serial.println();
  Serial.println("==============================");
  Serial.println("MICROPHONE RECORDING");
  Serial.println("==============================");

  if (
    cameraIsRunning
  ) {

    Serial.println(
      "ERROR: Camera still active"
    );

    return false;
  }

  if (
    SD_MMC.exists(
      filename
    )
  ) {

    SD_MMC.remove(
      filename
    );
  }

  File recording =
    SD_MMC.open(
      filename,
      FILE_WRITE
    );

  if (
    !recording
  ) {

    Serial.println(
      "Could not create recording"
    );

    return false;
  }

  uint8_t blankHeader[44] =
    {0};

  recording.write(
    blankHeader,
    44
  );

  audio.end();

  delay(100);

  audio.setPins(
    AUDIO_BCLK,
    AUDIO_WS,
    SPEAKER_DOUT,
    MIC_DIN
  );

  if (!audio.begin(
        I2S_MODE_STD,
        MIC_SAMPLE_RATE,
        I2S_DATA_BIT_WIDTH_32BIT,
        I2S_SLOT_MODE_STEREO
      )) {

    recording.close();

    Serial.println(
      "MIC I2S FAILED"
    );

    return false;
  }

  Serial.println();

  Serial.println(
    "SPEAK NOW"
  );

  Serial.print(
    "Recording "
  );

  Serial.print(
    RECORD_SECONDS
  );

  Serial.println(
    " seconds..."
  );

  uint32_t samplesNeeded =
    MIC_SAMPLE_RATE *
    RECORD_SECONDS;

  uint32_t samplesRecorded =
    0;

  uint32_t dataBytes =
    0;

  int32_t micFrames[
    256 * 2
  ];

  int16_t pcm16[256];

  while (
    samplesRecorded <
    samplesNeeded
  ) {

    size_t bytesRead =
      audio.readBytes(
        (char*)micFrames,
        sizeof(micFrames)
      );

    if (
      bytesRead == 0
    ) {

      continue;
    }

    int frames =
      bytesRead /
      (
        2 *
        sizeof(int32_t)
      );

    int outputSamples =
      0;

    for (
      int i = 0;
      i < frames &&
      samplesRecorded <
        samplesNeeded;
      i++
    ) {

      int32_t raw =
        micFrames[
          i * 2
        ];

      int32_t converted =
        raw >> 14;

      if (
        converted >
        32767
      ) {

        converted =
          32767;
      }

      if (
        converted <
        -32768
      ) {

        converted =
          -32768;
      }

      pcm16[
        outputSamples
      ] =
        (int16_t)
        converted;

      outputSamples++;

      samplesRecorded++;
    }

    if (
      outputSamples >
      0
    ) {

      size_t written =
        recording.write(
          (uint8_t*)pcm16,
          outputSamples *
          sizeof(int16_t)
        );

      dataBytes +=
        written;
    }
  }

  audio.end();

  writeWavHeader(
    recording,
    dataBytes
  );

  recording.close();

  Serial.println();

  Serial.println(
    "RECORDING COMPLETE"
  );

  Serial.print(
    "Recorded bytes: "
  );

  Serial.println(
    dataBytes
  );

  return true;
}

// ======================================================
// DOWNLOAD BACKEND AUDIO
// ======================================================

bool downloadReplyAudio(
  const String& audioPath,
  const char* savePath
) {

  if (
    WiFi.status() !=
    WL_CONNECTED
  ) {

    connectWiFi();
  }

  String fullURL =
    "http://172.20.10.6:5001" +
    audioPath;

  Serial.println();
  Serial.println("==============================");
  Serial.println("DOWNLOADING AI AUDIO");
  Serial.println("==============================");

  Serial.print(
    "GET "
  );

  Serial.println(
    fullURL
  );

  HTTPClient http;

  http.setTimeout(
    60000
  );

  if (
    !http.begin(
      fullURL
    )
  ) {

    Serial.println(
      "Could not start audio GET"
    );

    return false;
  }

  int code =
    http.GET();

  Serial.print(
    "HTTP STATUS: "
  );

  Serial.println(
    code
  );

  if (
    code != 200
  ) {

    http.end();

    return false;
  }

  if (
    SD_MMC.exists(
      savePath
    )
  ) {

    SD_MMC.remove(
      savePath
    );
  }

  File file =
    SD_MMC.open(
      savePath,
      FILE_WRITE
    );

  if (
    !file
  ) {

    http.end();

    return false;
  }

  WiFiClient* stream =
    http.getStreamPtr();

  uint8_t buffer[1024];

  int remaining =
    http.getSize();

  size_t total =
    0;

  while (
    http.connected() &&
    (
      remaining > 0 ||
      remaining == -1
    )
  ) {

    size_t available =
      stream->available();

    if (
      available
    ) {

      size_t amountToRead =
        min(
          available,
          sizeof(buffer)
        );

      int amount =
        stream->readBytes(
          (char*)buffer,
          amountToRead
        );

      if (
        amount > 0
      ) {

        file.write(
          buffer,
          amount
        );

        total +=
          amount;

        if (
          remaining > 0
        ) {

          remaining -=
            amount;
        }
      }
    }

    delay(1);
  }

  file.close();

  http.end();

  Serial.print(
    "Downloaded bytes: "
  );

  Serial.println(
    total
  );

  return (
    total > 0
  );
}

// ======================================================
// PLAY IMAGE ANALYSIS RESPONSE
// ======================================================

bool playImageAnalysisReply(
  const String& imageResponse
) {

  String replyText =
    extractJsonString(
      imageResponse,
      "reply_text"
    );

  String replyAudioURL =
    extractJsonString(
      imageResponse,
      "reply_audio_url"
    );

  String imageAction =
    extractJsonString(
      imageResponse,
      "action"
    );

  Serial.println();
  Serial.println("==============================");
  Serial.println("CAMERA AI RESPONSE");
  Serial.println("==============================");

  if (replyText.length() > 0) {

    Serial.print("Amy: ");

    Serial.println(replyText);
  }

  Serial.print("IMAGE ACTION: ");

  Serial.println(imageAction);

  if (replyAudioURL.length() == 0) {

    Serial.println(
      "Image response has no reply_audio_url"
    );

    shutdownCamera();

    restoreRFID();

    return false;
  }

  Serial.print(
    "Image reply audio: "
  );

  Serial.println(
    replyAudioURL
  );

  shutdownCamera();

  delay(400);

  if (!downloadReplyAudio(
        replyAudioURL,
        "/image_reply.wav"
      )) {

    Serial.println(
      "Image reply audio download failed"
    );

    restoreRFID();

    return false;
  }

  Serial.println();

  Serial.println(
    "PLAYING CAMERA RESPONSE"
  );

  if (!playWav(
        "/image_reply.wav"
      )) {

    Serial.println(
      "Camera response playback failed"
    );

    restoreRFID();

    return false;
  }

  Serial.println();

  Serial.println(
    "CAMERA RESPONSE PLAYED"
  );

  if (
    imageAction ==
    "log_checkout"
  ) {

    endPatientSession();

    return true;
  }

  restoreRFID();

  Serial.println();

  Serial.println(
    "Conversation paused."
  );

  Serial.println(
    "Tap the SAME wristband when the patient wants to reply."
  );

  return true;
}

// ======================================================
// SEND PATIENT AUDIO TO /voice
// ======================================================

bool sendVoiceFile(
  const char* filename
) {

  File audioFile =
    SD_MMC.open(
      filename,
      FILE_READ
    );

  if (
    !audioFile
  ) {

    Serial.println(
      "Could not open patient audio"
    );

    return false;
  }

  String path =
    "/api/triage/session/" +
    currentSessionID +
    "/voice";

  String boundary =
    "----GREENROVERVOICE";

  String head =
    "--" +
    boundary +
    "\r\n"
    "Content-Disposition: form-data; "
    "name=\"audio\"; "
    "filename=\"patient_audio.wav\"\r\n"
    "Content-Type: audio/wav\r\n"
    "\r\n";

  String tail =
    "\r\n--" +
    boundary +
    "--\r\n";

  size_t contentLength =
    head.length() +
    audioFile.size() +
    tail.length();

  Serial.println();
  Serial.println("==============================");
  Serial.println("POSTING PATIENT VOICE");
  Serial.println("==============================");

  Serial.print(
    "POST "
  );

  Serial.println(
    path
  );

  WiFiClient client;

  if (!client.connect(
        SERVER_IP,
        SERVER_PORT
      )) {

    audioFile.close();

    Serial.println(
      "Backend connection failed"
    );

    return false;
  }

  client.print(
    "POST " +
    path +
    " HTTP/1.1\r\n"
  );

  client.print(
    "Host: " +
    String(SERVER_IP) +
    ":" +
    String(SERVER_PORT) +
    "\r\n"
  );

  client.print(
    "Content-Type: multipart/form-data; boundary=" +
    boundary +
    "\r\n"
  );

  client.print(
    "Content-Length: "
  );

  client.print(
    contentLength
  );

  client.print(
    "\r\n"
  );

  client.print(
    "Connection: close\r\n"
  );

  client.print(
    "\r\n"
  );

  client.print(
    head
  );

  uint8_t buffer[1024];

  while (
    audioFile.available()
  ) {

    size_t bytesRead =
      audioFile.read(
        buffer,
        sizeof(buffer)
      );

    if (
      bytesRead >
      0
    ) {

      size_t written =
        client.write(
          buffer,
          bytesRead
        );

      if (
        written !=
        bytesRead
      ) {

        Serial.println(
          "Patient audio upload interrupted"
        );

        audioFile.close();

        client.stop();

        return false;
      }
    }
  }

  audioFile.close();

  client.print(
    tail
  );

  Serial.println(
    "Patient audio uploaded"
  );

  Serial.println(
    "Waiting for AI response..."
  );

  unsigned long started =
    millis();

  while (
    client.connected() &&
    !client.available()
  ) {

    if (
      millis() -
      started >
      120000
    ) {

      Serial.println(
        "VOICE REQUEST TIMEOUT"
      );

      client.stop();

      return false;
    }

    delay(10);
  }

  String response =
    "";

  while (
    client.connected() ||
    client.available()
  ) {

    while (
      client.available()
    ) {

      response +=
        (char)client.read();
    }

    delay(1);
  }

  client.stop();

  Serial.println();
  Serial.println("==============================");
  Serial.println("VOICE RESPONSE");
  Serial.println("==============================");

  Serial.println(
    response
  );

  bool success =
    (
      response.indexOf(
        "HTTP/1.1 200"
      ) >= 0
      ||
      response.indexOf(
        "HTTP/1.0 200"
      ) >= 0
    );

  if (
    !success
  ) {

    Serial.println(
      "VOICE POST FAILED"
    );

    return false;
  }

  Serial.println(
    "VOICE POST SUCCESS"
  );

  String action =
    extractJsonString(
      response,
      "action"
    );

  String replyText =
    extractJsonString(
      response,
      "reply_text"
    );

  String replyAudioURL =
    extractJsonString(
      response,
      "reply_audio_url"
    );

  Serial.println();

  Serial.print(
    "AI ACTION: "
  );

  Serial.println(
    action
  );

  if (
    replyText.length() >
    0
  ) {

    Serial.print(
      "Amy: "
    );

    Serial.println(
      replyText
    );
  }

  if (
    replyAudioURL.length() ==
    0
  ) {

    Serial.println(
      "reply_audio_url missing"
    );

    return false;
  }

  Serial.print(
    "Reply audio: "
  );

  Serial.println(
    replyAudioURL
  );

  if (!downloadReplyAudio(
        replyAudioURL,
        "/reply.wav"
      )) {

    Serial.println(
      "AI audio download failed"
    );

    return false;
  }

  Serial.println();

  Serial.println(
    "PLAYING AI RESPONSE"
  );

  if (!playWav(
        "/reply.wav"
      )) {

    Serial.println(
      "AI playback failed"
    );

    return false;
  }

  Serial.println();

  Serial.println(
    "AI RESPONSE PLAYED"
  );

  // ====================================================
  // END CONVERSATION IF BACKEND CHECKS PATIENT OUT
  // ====================================================

  if (
    action ==
    "log_checkout"
  ) {

    endPatientSession();

    return true;
  }

  // ====================================================
  // CAMERA REQUEST
  // ====================================================

  if (
    action ==
    "request_camera"
  ) {

    Serial.println();
    Serial.println("==============================");
    Serial.println("AI REQUESTED CAMERA");
    Serial.println("==============================");

    audio.end();

    delay(200);

    shutdownRFIDForCamera();

    if (!initCamera()) {

      Serial.println(
        "Camera initialization failed"
      );

      return false;
    }

    String imageResponse =
      captureAndSendPatientImage();

    if (
      imageResponse.length() ==
      0
    ) {

      Serial.println(
        "Camera/image API failed"
      );

      shutdownCamera();

      return false;
    }

    if (!playImageAnalysisReply(
          imageResponse
        )) {

      Serial.println(
        "Could not play image analysis response"
      );

      return false;
    }
  }

  return true;
}

// ======================================================
// ONE VOICE TURN
// ======================================================

void runVoiceTurn() {

  Serial.println();
  Serial.println("==============================");
  Serial.println("STARTING VOICE TURN");
  Serial.println("==============================");

  Serial.println(
    "Get ready to speak..."
  );

  Serial.println(
    "Recording will run for 5 seconds."
  );

  delay(1000);

  if (!recordPatientAudio(
        "/patient_audio.wav"
      )) {

    Serial.println(
      "Recording failed"
    );

    return;
  }

  if (!sendVoiceFile(
        "/patient_audio.wav"
      )) {

    Serial.println(
      "VOICE TURN FAILED"
    );

    return;
  }

  Serial.println();

  Serial.println(
    "VOICE TURN COMPLETE"
  );

  if (
    patientSessionActive
  ) {

    // NEW:
    // Amy has finished speaking.
    // Start the 10-second response timer now.
    conversationPausedAt =
      millis();

    Serial.println();

    Serial.println(
      "Conversation paused."
    );

    Serial.println(
      "Tap the SAME wristband when the patient wants to reply."
    );
  }
}

// ======================================================
// SETUP
// ======================================================

void setup() {

  Serial.begin(
    115200
  );

  delay(1500);

  Serial.println();
  Serial.println("=================================");
  Serial.println("GREEN ROVER TRIAGE");
  Serial.println("=================================");

  // ====================================================
  // RFID
  // ====================================================

  SPI.begin(
    RFID_SCK_PIN,
    RFID_MISO_PIN,
    RFID_MOSI_PIN,
    RFID_SS_PIN
  );

  rfid.PCD_Init();

  delay(100);

  byte version =
    rfid.PCD_ReadRegister(
      MFRC522::VersionReg
    );

  Serial.print(
    "MFRC522 Version: 0x"
  );

  Serial.println(
    version,
    HEX
  );

  if (
    version == 0x00 ||
    version == 0xFF
  ) {

    Serial.println(
      "RFID NOT DETECTED"
    );
  }

  else {

    Serial.println(
      "RFID READY"
    );
  }

  // ====================================================
  // WIFI
  // ====================================================

  connectWiFi();

  // ====================================================
  // SD
  // ====================================================

  Serial.println();

  Serial.println(
    "Mounting SD card..."
  );

  if (!SD_MMC.begin(
        "/sdcard",
        true
      )) {

    Serial.println(
      "SD MOUNT FAILED"
    );

    return;
  }

  Serial.println(
    "SD mounted"
  );

  // ====================================================
  // AMY INTRO
  // ====================================================

  if (
    SD_MMC.exists(
      "/amy_intro.wav"
    )
  ) {

    Serial.println(
      "amy_intro.wav found"
    );

    playWav(
      "/amy_intro.wav"
    );
  }

  else {

    Serial.println(
      "amy_intro.wav missing"
    );
  }

  // ====================================================
  // WAIT FOR PATIENT
  // ====================================================

  Serial.println();
  Serial.println("=================================");
  Serial.println("WAITING FOR PATIENT WRISTBAND");
  Serial.println("Tap NFC sticker...");
  Serial.println("=================================");
}

// ======================================================
// LOOP
// ======================================================

void loop() {

  // ====================================================
  // ACTIVE PATIENT
  // ====================================================

  if (
    patientSessionActive
  ) {

    // ==================================================
    // NEW:
    // 10 SECOND CONVERSATION TIMEOUT
    // ==================================================

    if (
      conversationPausedAt > 0 &&
      millis() -
      conversationPausedAt >=
      CONVERSATION_TIMEOUT_MS
    ) {

      Serial.println();
      Serial.println("=================================");
      Serial.println("CONVERSATION TIMEOUT");
      Serial.println("No response for 10 seconds.");
      Serial.println("Ending patient session.");
      Serial.println("=================================");

      conversationPausedAt = 0;

      // Remember that THIS session ended because
      // of inactivity.
      sessionTimedOut = true;

      endPatientSession();

      return;
    }

    // Camera may have temporarily taken the RFID pins.

    if (
      rfidReleased &&
      !cameraIsRunning
    ) {

      restoreRFID();
    }

    if (
      !rfid.PICC_IsNewCardPresent()
    ) {

      delay(20);

      return;
    }

    if (
      !rfid.PICC_ReadCardSerial()
    ) {

      delay(20);

      return;
    }

    String uid =
      getUIDString();

    rfid.PICC_HaltA();

    rfid.PCD_StopCrypto1();

    Serial.println();
    Serial.println("=================================");
    Serial.println("WRISTBAND TAPPED FOR NEXT TURN");
    Serial.println("=================================");

    Serial.print(
      "UID: "
    );

    Serial.println(
      uid
    );

    // Keep one patient's conversation attached
    // to one session.

    if (
      uid !=
      currentPatientUID
    ) {

      Serial.println(
        "Different wristband detected."
      );

      Serial.println(
        "Current patient session is still active."
      );

      Serial.println(
        "Tap the SAME wristband to reply."
      );

      delay(1000);

      return;
    }

    Serial.println(
      "Same patient confirmed."
    );

    Serial.println(
      "Starting next voice turn..."
    );

    // Patient responded before 10 seconds.
    // Cancel the timeout timer.

    conversationPausedAt = 0;

    runVoiceTurn();

    return;
  }

  // ====================================================
  // NO ACTIVE PATIENT:
  // WAIT FOR A WRISTBAND TO START A NEW SESSION
  // ====================================================

  if (
    !rfid.PICC_IsNewCardPresent()
  ) {

    delay(20);

    return;
  }

  if (
    !rfid.PICC_ReadCardSerial()
  ) {

    delay(20);

    return;
  }

  String uid =
    getUIDString();

  Serial.println();
  Serial.println("=================================");
  Serial.println("WRISTBAND DETECTED");
  Serial.println("=================================");

  Serial.print(
    "UID: "
  );

  Serial.println(
    uid
  );

  rfid.PICC_HaltA();

  rfid.PCD_StopCrypto1();

  if (
    !createPatientSession(
      uid
    )
  ) {

    Serial.println(
      "SESSION CREATION FAILED"
    );

    delay(1000);

    return;
  }

  // ====================================================
  // NEW:
  // AFTER-TIMEOUT PATIENT GREETING
  //
  // If the previous patient disappeared and their
  // conversation timed out, the NEXT successful new
  // patient scan gets the new prerecorded greeting.
  // ====================================================

  if (
    sessionTimedOut
  ) {

    Serial.println();
    Serial.println("==============================");
    Serial.println("PLAYING AFTER-TIMEOUT GREETING");
    Serial.println("==============================");

    if (
      SD_MMC.exists(
        "/amy_after_timeout.wav"
      )
    ) {

      Serial.println(
        "amy_after_timeout.wav found"
      );

      playWav(
        "/amy_after_timeout.wav"
      );
    }

    else {

      Serial.println(
        "amy_after_timeout.wav missing"
      );
    }

    // Only play the greeting once.
    sessionTimedOut = false;
  }

  // First turn begins immediately after the wristband
  // starts the session.

  runVoiceTurn();
}