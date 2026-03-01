import os
import sys
import time
import serial
import serial.tools.list_ports
import traceback
from adafruit_pn532.uart import PN532_UART

def list_available_ports():
    ports = list(serial.tools.list_ports.comports())
    print("Available serial ports:")
    for p in ports:
        print(f"  {p.device} ({p.description})")

def main():
    # Detect serial port
    # We found /dev/cu.usbserial-1440 earlier
    port = "/dev/cu.usbserial-1440"
    
    if not os.path.exists(port):
        print(f"Error: {port} not found. Please check connection.")
        list_available_ports()
        return

    print(f"Connecting to PN532 on {port}...")
    
    uart = None
    try:
        # Initialize the serial connection
        # PN532 usually uses 115200 for UART. Some boards use 9600.
        try:
            uart = serial.Serial(port, baudrate=115200, timeout=1.0)
            pn532 = PN532_UART(uart, debug=False)
            
            # Get firmware version to verify connection
            print("Sending firmware version request (115200 baud)...")
            version = pn532.firmware_version
        except Exception:
            version = None

        if version is None:
            print("Trying 9600 baud...")
            if uart:
                uart.close()
            uart = serial.Serial(port, baudrate=9600, timeout=1.0)
            pn532 = PN532_UART(uart, debug=False)
            version = pn532.firmware_version
            
        if version:
            ic, ver, rev, support = version
            print(f"Found PN532 (IC: 0x{ic:02X}) with firmware version: {ver}.{rev}")
            
            # Configure PN532 to communicate with tags
            pn532.SAM_configuration()
            
            print("\n" + "="*40)
            print("Waiting for an NFC tag...")
            print("(Press Ctrl+C to stop)")
            print("="*40)
            
            last_uid = None
            while True:
                # Check if a card is available to read
                uid = pn532.read_passive_target(timeout=0.5)
                
                if uid is not None:
                    if uid != last_uid:
                        print(f"\n[SCAN] Found card!")
                        print(f"  UID: {' '.join(['%02X' % i for i in uid])}")
                        print(f"  UID Hex: {uid.hex().upper()}")
                        print(f"  UID Length: {len(uid)} bytes")
                        last_uid = uid
                else:
                    if last_uid is not None:
                        print("\n[INFO] Card removed.")
                        last_uid = None
                    
                time.sleep(0.1)
        else:
            print("Failed to initialize PN532. Check connections and ensure it's in UART mode.")
            print("Possible issues:")
            print("1. The device is not in UART mode (check dip switches/jumpers).")
            print("2. Driver issue (CH340 driver might be required on some macOS versions).")

    except KeyboardInterrupt:
        print("\nStopping...")
    except Exception as e:
        print(f"\nAn error occurred: {e}")
        traceback.print_exc()
    finally:
        if uart is not None:
            uart.close()

if __name__ == "__main__":
    main()
