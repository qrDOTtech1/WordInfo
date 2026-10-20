# SHW OS - Kernel
# Initializes all hardware, mounts SD, launches UI shell
import gc
import machine
import time

from system.hal import HAL
from system.sdcard import SDManager
from system.app_manager import AppManager
from ui.shell import Shell


class Kernel:
    def __init__(self):
        self.hal = None
        self.sd = None
        self.shell = None
        self.app_manager = None

    def boot(self):
        print("[SHW OS] Booting...")

        # Init hardware abstraction (display + touch)
        self.hal = HAL()
        self.hal.init()
        print("[SHW OS] HAL ready")

        # Show splash
        self.hal.display.fill(0x0000)
        self.hal.display.text("SHW OS", 100, 100, 0xFFFF)
        self.hal.display.text("Booting...", 90, 130, 0x07E0)
        self.hal.display.show()

        # Mount SD card
        self.sd = SDManager()
        if self.sd.mount():
            print("[SHW OS] SD card mounted at /sd")
        else:
            print("[SHW OS] WARNING: No SD card")

        # App manager scans /sd/apps for installed apps
        self.app_manager = AppManager(self.sd)
        self.app_manager.scan()

        # Create shell (home screen UI)
        self.shell = Shell(self.hal, self.app_manager)

        gc.collect()
        print("[SHW OS] Boot complete. Free RAM:", gc.mem_free())

    def run(self):
        self.shell.run()
