# SHW OS - SD Card Manager
import machine
import uos


# === SD CARD SPI PINS - EDIT FOR YOUR BOARD ===
SD_SPI_ID = 2
SD_SCK = 18
SD_MOSI = 23
SD_MISO = 19
SD_CS = 5


class SDManager:
    def __init__(self):
        self.mounted = False

    def mount(self):
        try:
            from drivers.sdcard_driver import SDCard

            spi = machine.SPI(
                SD_SPI_ID,
                baudrate=1_000_000,
                sck=machine.Pin(SD_SCK),
                mosi=machine.Pin(SD_MOSI),
                miso=machine.Pin(SD_MISO),
            )
            sd = SDCard(spi, machine.Pin(SD_CS, machine.Pin.OUT))
            uos.mount(sd, "/sd")
            self._ensure_dirs()
            self.mounted = True
            return True
        except Exception as e:
            print("[SD] Mount failed:", e)
            return False

    def _ensure_dirs(self):
        for d in ("/sd/apps", "/sd/lib", "/sd/data", "/sd/config", "/sd/ota"):
            try:
                uos.mkdir(d)
            except OSError:
                pass

    def list_dir(self, path="/sd"):
        try:
            return uos.listdir(path)
        except:
            return []
