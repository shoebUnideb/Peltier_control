import pyvisa
import serial
import time
import pandas as pd
from datetime import datetime

class PT100TempLogger:
    def __init__(self, port='COM12', baudrate=9600):
        """Initialize the serial connection with Arduino"""
        try:
            self.ser = serial.Serial(port=port, baudrate=baudrate, timeout=1)
            time.sleep(2)  # Allow time for serial connection to establish
        except serial.SerialException as e:
            print(f"Error opening serial port: {e}")
            raise

    def read_temperature(self):
        """Read temperature data from Arduino"""
        try:
            self.ser.write(b'r')  # Send read command to Arduino
            if self.ser.in_waiting:
                line = self.ser.readline().decode('utf-8').strip()
                try:
                    return float(line)
                except ValueError:
                    print(f"Invalid data received: {line}")
                    return None
        except serial.SerialException as e:
            print(f"Error reading from serial port: {e}")
            return None

    def __del__(self):
        """Cleanup: Close serial connection"""
        if hasattr(self, 'ser') and self.ser.is_open:
            self.ser.close()

def control_power_supply_with_temp_monitoring():
    # Initialize temperature logger
    temp_logger = PT100TempLogger(port='COM12')  # Adjust COM port as needed
    data = []  # To store all measurements
    
    try:
        # Create a resource manager for power supply
        rm = pyvisa.ResourceManager()
        ps = rm.open_resource('COM10')
        
        # Initialize the power supply
        ps.write('*RST')
        ps.write('SYST:REM')
        
        # Set parameters
        target_voltage = 6.0
        rate = 0.05
        duration = 120  # 2 minutes
        
        # Calculate steps
        steps = int(target_voltage / rate)
        voltage_step = rate
        
        # Ramp up voltage
        current_voltage = 0
        for step in range(steps):
            current_voltage += voltage_step
            
            # Set voltage
            ps.write(f'VOLT {current_voltage:.3f}')
            ps.write('OUTP ON')
            
            # Read power supply measurements
            actual_voltage = float(ps.query('MEAS:VOLT?'))
            actual_current = float(ps.query('MEAS:CURR?'))
            
            # Read temperature
            temperature = temp_logger.read_temperature()
            timestamp = datetime.now()
            
            # Store data
            data.append({
                'Timestamp': timestamp,
                'Set_Voltage': current_voltage,
                'Measured_Voltage': actual_voltage,
                'Measured_Current': actual_current,
                'Temperature': temperature
            })
            
            # Display measurements
            print(f'\nStep {step + 1}/{steps}:')
            print(f'Time: {timestamp.strftime("%H:%M:%S")}')
            print(f'Set Voltage: {current_voltage:.3f}V')
            print(f'Measured Voltage: {actual_voltage:.3f}V')
            print(f'Measured Current: {actual_current:.3f}A')
            print(f'Temperature: {temperature:.2f}°C' if temperature is not None else 'Temperature: Reading Error')
            print('-' * 50)
            
            time.sleep(1)
        
        # Maintain at target voltage for remaining time
        remaining_time = duration - steps
        start_time = time.time()
        
        while (time.time() - start_time) < remaining_time:
            # Toggle output
            ps.write('OUTP OFF')
            time.sleep(0.5)
            ps.write('OUTP ON')
            time.sleep(0.5)
            
            # Read measurements
            actual_voltage = float(ps.query('MEAS:VOLT?'))
            actual_current = float(ps.query('MEAS:CURR?'))
            temperature = temp_logger.read_temperature()
            timestamp = datetime.now()
            
            # Store data
            data.append({
                'Timestamp': timestamp,
                'Set_Voltage': target_voltage,
                'Measured_Voltage': actual_voltage,
                'Measured_Current': actual_current,
                'Temperature': temperature
            })
            
            # Display measurements
            elapsed = time.time() - start_time
            print(f'\nTime remaining: {remaining_time - elapsed:.1f}s')
            print(f'Time: {timestamp.strftime("%H:%M:%S")}')
            print(f'Measured Voltage: {actual_voltage:.3f}V')
            print(f'Measured Current: {actual_current:.3f}A')
            print(f'Temperature: {temperature:.2f}°C' if temperature is not None else 'Temperature: Reading Error')
            print('-' * 50)
        
        # Save data to Excel
        df = pd.DataFrame(data)
        filename = f'power_supply_temp_log_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
        df.to_excel(filename, index=False)
        print(f"\nData logged successfully to {filename}")
        
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        # Save data even if there's an error
        if data:
            df = pd.DataFrame(data)
            filename = f'power_supply_temp_log_ERROR_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx'
            df.to_excel(filename, index=False)
            print(f"Partial data saved to {filename}")
    
    finally:
        # Cleanup
        try:
            ps.write('OUTP OFF')
            ps.close()
            rm.close()
        except:
            pass

if __name__ == "__main__":
    control_power_supply_with_temp_monitoring()