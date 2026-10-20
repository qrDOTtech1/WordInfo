# SHW OS - Hardware Abstraction Layer
# Centralizes display + touch driver init
# ADAPT PINS TO YOUR JCX BOARD
import machine
from drivers.ili9341 import ILI9341
from drivers.xpt2046 import XPT2046


# === PIN CONFIG - EDIT FOR YOUR BOARD ===
# Display SPI
DISPLAY_SPI_ID = 1
DISPLAY_SCK = 14
DISPLAY_MOSI = 13
DISPLAY_MISO = 12
DISPLAY_CS = 15
DISPLAY_DC = 2
DISPLAY_RST = -1
DISPLAY_BL = 21

# Touch SPI (can share SPI bus or use separate)
TOUCH_SPI_ID = 1
TOUCH_CS = 33
TOUCH_IRQ = 36

# Screen dimensions
SCREEN_W = 320
SCREEN_H = 240


class HAL:
    def __init__(self):
        self.display = None
        self.touch = None
        self.backlight = None

    def init(self):
        # Backlight
        if DISPLAY_BL >= 0:
            self.backlight = machine.PWM(machine.Pin(DISPLAY_BL), freq=1000)
            self.backlight.duty(1023)

        # SPI bus for display
        spi = machine.SPI(
            DISPLAY_SPI_ID,
            baudrate=40_000_000,
            polarity=0,
            phase=0,
            sck=machine.Pin(DISPLAY_SCK),
            mosi=machine.Pin(DISPLAY_MOSI),
            miso=machine.Pin(DISPLAY_MISO),
        )

        # Display driver
        self.display = ILI9341(
            spi,
            cs=machine.Pin(DISPLAY_CS, machine.Pin.OUT),
            dc=machine.Pin(DISPLAY_DC, machine.Pin.OUT),
            rst=machine.Pin(DISPLAY_RST, machine.Pin.OUT) if DISPLAY_RST >= 0 else None,
            w=SCREEN_W,
            h=SCREEN_H,
        )

        # Touch driver (shares SPI bus, slower baudrate for touch)
        self.touch = XPT2046(
            spi,
            cs=machine.Pin(TOUCH_CS, machine.Pin.OUT),
            irq=machine.Pin(TOUCH_IRQ, machine.Pin.IN),
            w=SCREEN_W,
            h=SCREEN_H,
        )

    def set_brightness(self, level):
        if self.backlight:
            self.backlight.duty(int(level * 1023 / 100))

    def get_touch(self):
        return self.touch.get_touch()
