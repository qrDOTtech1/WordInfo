# SHW OS - WiFi Manager
import network
import time
import json


WIFI_CONFIG = "wifi.json"


class WiFiManager:
    def __init__(self):
        self.sta = network.WLAN(network.STA_IF)

    def load_config(self):
        try:
            with open(WIFI_CONFIG, "r") as f:
                return json.load(f)
        except:
            return None

    def save_config(self, ssid, password):
        with open(WIFI_CONFIG, "w") as f:
            json.dump({"ssid": ssid, "password": password}, f)

    def connect(self, ssid=None, password=None, timeout=10):
        if ssid is None:
            cfg = self.load_config()
            if not cfg:
                return False
            ssid = cfg["ssid"]
            password = cfg["password"]

        self.sta.active(True)
        self.sta.connect(ssid, password)

        for _ in range(timeout * 2):
            if self.sta.isconnected():
                print("[WiFi] Connected:", self.sta.ifconfig()[0])
                return True
            time.sleep(0.5)

        print("[WiFi] Failed to connect")
        return False

    def disconnect(self):
        self.sta.disconnect()
        self.sta.active(False)

    @property
    def connected(self):
        return self.sta.isconnected()

    @property
    def ip(self):
        return self.sta.ifconfig()[0] if self.sta.isconnected() else None

    def scan(self):
        self.sta.active(True)
        return [(s[0].decode(), s[3]) for s in self.sta.scan()]
