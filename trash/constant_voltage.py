import pyvisa
import serial
import time
import pandas as pd
import matplotlib.pyplot as plt
import os
from datetime import datetime

class PT100TempLogger:
    def __init__(self, port='COM12', baudrate=9600):
        """Initialize the serial connection with Arduino"""
        try:
            self.ser = serial.Serial(port=port, baudrate=baudrate, timeout=1)
            time.sleep(2)
        except serial.SerialException as e:
            print(f"Error opening serial port: {e}")
            raise

    def read_temperature(self):
        """Read temperature data from Arduino"""
        try:
            self.ser.write(b'r')
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

def create_plots(df, output_folder):
    """Create and save plots from the measurement data"""
    df['Seconds'] = (df['Timestamp'] - df['Timestamp'].iloc[0]).dt.total_seconds()
    plt.figure(figsize=(15, 10))

    plt.subplot(2, 2, 1)
    plt.plot(df['Seconds'], df['Temperature'], 'b-')
    plt.xlabel('Time (seconds)')
    plt.ylabel('Temperature (°C)')
    plt.title('Temperature vs Time')
    plt.grid(True)

    plt.subplot(2, 2, 2)
    plt.plot(df['Set_Voltage'], df['Temperature'], 'r-')
    plt.xlabel('Set Voltage (V)')
    plt.ylabel('Temperature (°C)')
    plt.title('Temperature vs Set Voltage')
    plt.grid(True)

    plt.subplot(2, 2, 3)
    plt.plot(df['Measured_Voltage'], df['Temperature'], 'g-')
    plt.xlabel('Measured Voltage (V)')
    plt.ylabel('Temperature (°C)')
    plt.title('Temperature vs Measured Voltage')
    plt.grid(True)

    plt.subplot(2, 2, 4)
    plt.plot(df['Measured_Current'], df['Temperature'], 'm-')
    plt.xlabel('Current (A)')
    plt.ylabel('Temperature (°C)')
    plt.title('Temperature vs Current')
    plt.grid(True)

    plt.tight_layout()
    plot_file = os.path.join(output_folder, 'measurement_plots.png')
    plt.savefig(plot_file, dpi=300, bbox_inches='tight')
    plt.close()

    return plot_file

def control_power_supply_with_temp_monitoring():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_folder = f'measurements_{timestamp}'
    os.makedirs(output_folder, exist_ok=True)
    
    temp_logger = PT100TempLogger(port='COM12')
    data = []
    
    TARGET_TEMPERATURE = 20.0
    reading_counter = 0
    target_reached = False
    
    try:
        rm = pyvisa.ResourceManager()
        ps = rm.open_resource('COM10')
        
        ps.write('*RST')
        ps.write('SYST:REM')
        
        target_voltage = 6.0
        rate = 0.05
        duration = 120
        
        steps = int(target_voltage / rate)
        voltage_step = rate
        
        # Ramp up voltage
        current_voltage = 0
        start_time = time.time()
        elapsed_time = 0
        
        while elapsed_time < duration:
            if not target_reached and current_voltage < target_voltage:
                current_voltage = min(current_voltage + voltage_step, target_voltage)
            
            ps.write(f'VOLT {current_voltage:.3f}')
            ps.write('OUTP ON')
            
            actual_voltage = float(ps.query('MEAS:VOLT?'))
            actual_current = float(ps.query('MEAS:CURR?'))
            temperature = temp_logger.read_temperature()
            timestamp = datetime.now()
            
            data.append({
                'Timestamp': timestamp,
                'Set_Voltage': current_voltage,
                'Measured_Voltage': actual_voltage,
                'Measured_Current': actual_current,
                'Temperature': temperature
            })
            
            print(f'\nElapsed time: {elapsed_time:.1f}s')
            print(f'Time: {timestamp.strftime("%H:%M:%S")}')
            print(f'Set Voltage: {current_voltage:.3f}V')
            print(f'Measured Voltage: {actual_voltage:.3f}V')
            print(f'Measured Current: {actual_current:.3f}A')
            print(f'Temperature: {temperature:.2f}°C' if temperature is not None else 'Temperature: Reading Error')
            print('-' * 50)
            
            reading_counter += 1
            if reading_counter > 3 and temperature is not None:
                if abs(temperature - TARGET_TEMPERATURE) <= 0.1:
                    if not target_reached:
                        print(f"\nTarget temperature {TARGET_TEMPERATURE}°C reached!")
                        target_reached = True
            
            time.sleep(1)
            elapsed_time = time.time() - start_time
        
        # Save data and create plots
        df = pd.DataFrame(data)
        excel_file = os.path.join(output_folder, f'power_supply_temp_log.xlsx')
        df.to_excel(excel_file, index=False)
        plot_file = create_plots(df, output_folder)
        
        print(f"\nMeasurement complete!")
        print(f"Data saved to: {excel_file}")
        print(f"Plots saved to: {plot_file}")
        
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        if data:
            df = pd.DataFrame(data)
            excel_file = os.path.join(output_folder, f'power_supply_temp_log_ERROR.xlsx')
            df.to_excel(excel_file, index=False)
            create_plots(df, output_folder)
            print(f"Partial data saved to: {output_folder}")
    
    finally:
        try:
            ps.write('OUTP OFF')
            ps.close()
            rm.close()
        except:
            pass

if __name__ == "__main__":
    control_power_supply_with_temp_monitoring()
