Milestone 1 — NodeMCU -> Flask

Files added:
- backend/app.py
- backend/requirements.txt
- arduino/nodemcu/nodemcu.ino

1) Install Python dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt
```

2) Run Flask on your Mac

```bash
python3 backend/app.py
```

Flask listens on 0.0.0.0:5000 so devices on the LAN can reach it.

3) Edit `arduino/nodemcu/nodemcu.ino` and set `ssid`, `password`, and `serverIp` to your Mac's IP.

Find your Mac IP (Wi‑Fi):

```bash
ipconfig getifaddr en0
```

If that returns nothing, try `ifconfig` and look for the `inet` address under `en0` or `en1`.

4) Upload sketch to NodeMCU via Arduino IDE (select the ESP8266 board package).

5) Open Serial Monitor at 115200 baud to view output.

Expected Flask terminal output:

```
INFO:werkzeug: * Running on http://0.0.0.0:5000/ (Press CTRL+C to quit)
INFO:root: Received: hello from rover
```

Expected NodeMCU Serial Monitor output:

```
NodeMCU starting...
Connecting to WiFi.....
WiFi connected, IP: 192.168.x.x
Sending POST to http://192.168.1.12:5000/api/test
HTTP status: 200
Response: {"status":"success","message":"hello from rover"}
```

Troubleshooting:
- Connection refused: ensure Flask is running and not blocked by the firewall.
- Timeout: ensure NodeMCU and Mac are on the same Wi‑Fi network and use the Mac IP, not localhost.
- Firewall: allow Python or disable firewall temporarily for testing in System Settings → Security & Privacy → Firewall.
- Wrong IP: re-run `ipconfig getifaddr en0` or check `ifconfig`.
- Wi‑Fi: ensure you use a 2.4GHz SSID and correct password (ESP8266 does not support some 5GHz networks).
