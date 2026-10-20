# SHW OS - Guition JC4827W543

MicroPython OS base for the **JC4827W543** (ESP32-S3, 4.3" 480x272, NV3041A QSPI, GT911 capacitive touch).

## Hardware
- **MCU:** ESP32-S3-WROOM-1-N4R8 (4MB Flash, 8MB PSRAM)
- **Display:** NV3041A 480x272 QSPI
- **Touch:** GT911 I2C capacitive
- **SD Card:** 8GB micro SD
- **Connectivity:** WiFi + BLE

## First Flash (USB)

### 1. Flash the LVGL MicroPython firmware
```bash
pip install esptool
python3 -m esptool --chip esp32s3 -p /dev/ttyUSB0 -b 460800 \
  --before default_reset --after hard_reset \
  write_flash --flash_mode dio --flash_size 4MB --flash_freq 80m \
  --erase-all 0x0 firmware/lvgl_micropy_ESP32_GENERIC_S3-SPIRAM_OCT-4.bin
```

### 2. Upload the OS files
Use **mpremote** or **Thonny** to upload these files to the board's root:
```
boot.py
main.py
lv_config.py
nv3041aG.py
_nv3041a_init_type3.py
test.py
wifi_manager.py
ota.py
```

### 3. Configure WiFi
In REPL:
```python
from wifi_manager import WiFiManager
w = WiFiManager()
w.save_config("YOUR_SSID", "YOUR_PASSWORD")
w.connect()
```

### 4. Configure OTA server
Edit `main.py` and change `YOUR_OTA_SERVER` to your server URL.

## OTA Updates

Host a simple HTTP server with this structure:
```
/manifest.json    - version + file list
/files/           - updated .py files
```

Example `manifest.json`:
```json
{
  "version": "1.1.0",
  "files": [
    {"src": "files/main.py", "dst": "main.py"},
    {"src": "files/test.py", "dst": "test.py"}
  ]
}
```

## Credits
- Display/Touch drivers from [straga/micropython_lcd](https://github.com/straga/micropython_lcd)
- LVGL MicroPython from [lvgl-micropython](https://github.com/lvgl-micropython/lvgl_micropython)
