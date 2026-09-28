import serial
import time
import pandas as pd
import matplotlib.pyplot as plt
import os
from datetime import datetime

class PT100TempLogger:
    def __init__(self, port, baudrate=9600):
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
            self.ser.reset_input_buffer()  # Drop stale replies so this reading is current
            self.ser.write(b'r')
            line = self.ser.readline().decode('utf-8', errors='replace').strip()
            try:
                # Firmware sends "pt100_temp,dht_humidity,dht_temp"; the first field is the plate temperature
                temperature = float(line.split(',')[0])
                if not -50 <= temperature <= 150:
                    # e.g. -244 °C means the MAX31865 read 0 ohm: Pt100 or MAX31865 disconnected
                    print(f"Implausible Pt100 reading {temperature:.2f}°C; check the Pt100 / MAX31865 wiring")
                    return None
                return temperature
            except ValueError:
                print(f"Invalid data received: {line!r}")
                return None
        except serial.SerialException as e:
            print(f"Error reading from serial port: {e}")
            return None

    def read_ambient_temperature(self):
        """Read the DHT22 air temperature (third field of the 'r' reply); None if the DHT22 read fails"""
        try:
            self.ser.reset_input_buffer()
            self.ser.write(b'r')
            line = self.ser.readline().decode('utf-8', errors='replace').strip()
            temperature = float(line.split(',')[2])
        except (serial.SerialException, ValueError, IndexError):
            return None
        return temperature if -20 <= temperature <= 60 else None  # Also rejects "nan" from a failed DHT22 read

    def set_bridge(self, command, duty=0):
        """Set the BTS7960 H-bridge: 'f' forward or 'b' backward at PWM duty 0-255, or 'o' off"""
        message, expected = ('o', 'OK o') if command == 'o' else (f'{command}{duty}\n', f'OK {command}{duty}')
        self.ser.reset_input_buffer()
        self.ser.write(message.encode())
        reply = self.ser.readline().decode('utf-8', errors='replace').strip()
        if reply != expected:
            raise RuntimeError(f"H-bridge command {command!r} not acknowledged (got {reply!r}); "
                               "is firmware/peltier_controller uploaded to the Arduino?")

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

def save_data(data, output_folder, error=False):
    """Save measurement data to Excel and create plots"""
    if data:
        df = pd.DataFrame(data)
        suffix = '_ERROR' if error else ''
        excel_file = os.path.join(output_folder, f'power_supply_temp_log{suffix}.xlsx')
        df.to_excel(excel_file, index=False)
        plot_file = create_plots(df, output_folder)
        return excel_file, plot_file
    return None, None

def pid_control(target_temp, current_temp, prev_temp, dt, Kp, Ki, Kd, integral, out_min, out_max,
                integral_band=None):
    """One PID step for a bidirectional (H-bridge) drive; gains are per second and `dt` is the time
    since the previous step (s).

    Returns (output, integral): a signed effort, positive cools and negative heats, limited to
    [out_min, out_max]. `integral` is kept in output units (V), so it can be preloaded with a
    feedforward estimate of the effort needed to hold the target. If `integral_band` is given, the
    integral pauses while the temperature is more than that many °C from the target and still moving
    toward it, so the large errors of the approach don't wind it up and cause overshoot. If the
    temperature stalls outside the band, the integral runs again so it can't get stuck there.
    """
    error = current_temp - target_temp  # Positive when too warm, i.e. cooling needed
    # Derivative of the measurement rather than the error, so a target change doesn't kick the output
    derivative = (current_temp - prev_temp) / dt if prev_temp is not None and dt > 0 else 0.0
    pd_term = Kp * error + Kd * derivative
    candidate = integral + Ki * error * dt
    output = pd_term + candidate

    # Prevent integral windup: don't integrate while the error pushes further into the limit
    winding_up = (output > out_max and error > 0) or (output < out_min and error < 0)
    approaching = integral_band is not None and abs(error) > integral_band and derivative * error < 0
    if not (winding_up or approaching):
        integral = candidate

    return max(out_min, min(pd_term + integral, out_max)), integral