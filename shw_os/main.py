# SHW OS - main.py
# Entry point - initializes hardware then launches the OS shell
import gc
import sys

sys.path.append("/sd/lib")
sys.path.append("/sd/apps")

from system.kernel import Kernel

def main():
    kernel = Kernel()
    kernel.boot()
    kernel.run()

main()
