# SHW OS - boot.py
# Runs at device startup before main.py
import gc
import machine

gc.collect()

# Disable debug REPL on UART0 if needed for other peripherals
# import uos
# uos.dupterm(None, 1)
