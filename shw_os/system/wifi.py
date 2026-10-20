# SHW OS - WiFi Manager
import network
import time
import ujson


CONFIG_PATH = "/sd/config/wifi.json"


class WiFiManager:
    def __init__(self):
        self.sta = network.WLAN(network.STA_IF)

    def load_config(self):
        try:
            with open(CONFIG_PATH, "r") as f:
                return ujson.load(f)
        except:
            return None

    def save_config(self, ssid, password):
        with open(CONFIG_PATH, "w") as f:
            ujson.dump({"ssid": ssid, "password": password}, f)

    def connect(self, ssid=None, password=None):
        if ssid is None:
            cfg = self.load_config()
            if cfg is None:
                return False
            ssid = cfg["ssid"]
            password = cfg["password"]

        self.sta.active(True)
        self.sta.connect(ssid, password)

        for _ in range(20):
            if self.sta.isconnected():
                print("[WiFi] Connected:", self.sta.ifconfig()[0])
                return True
            time.sleep(0.5)

        print("[WiFi] Connection failed")
        return False

    def disconnect(self):
        self.sta.disconnect()
        self.sta.active(False)

    def is_connected(self):
        return self.sta.isconnected()

    def ip(self):
        if self.sta.isconnected():
            return self.sta.ifconfig()[0]
        return None

    def scan(self):
        self.sta.active(True)
        return [(s[0].decode(), s[3]) for s in self.sta.scan()]
