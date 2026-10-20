# SHW OS - main.py
# Entry point for JC4827W543
# Phase 1: Init display+touch, connect WiFi, check OTA, run test UI
import gc

print("[SHW OS] Starting...")

# Step 1: Init display + touch (lv_config does this on import)
import lv_config
print("[SHW OS] Display + Touch OK")

# Step 2: WiFi + OTA check
try:
    from wifi_manager import WiFiManager
    wifi = WiFiManager()
    if wifi.connect():
        from ota import OTAUpdater
        ota = OTAUpdater("http://YOUR_OTA_SERVER")
        ota.check_and_apply()
    else:
        print("[SHW OS] No WiFi - skipping OTA")
except Exception as e:
    print("[SHW OS] WiFi/OTA error:", e)

gc.collect()

# Step 3: Launch test UI (replace with shell later)
import test
