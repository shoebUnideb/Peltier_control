import pyvisa
import time
import os
from datetime import datetime
import signal
import threading
from pid_utils import PT100TempLogger, save_data, pid_control
import serial.tools.list_ports

TARGET_TEMPERATURE = 19.5   # Default target (°C); the UI lets you change it before starting
TARGET_RANGE = (5.0, 40.0)  # Allowed targets (°C)
SAFE_RANGE = (0.0, 45.0)    # The run stops if the plate temperature leaves this range (°C)

# Peltier drive through the BTS7960 H-bridge. The bridge only works properly with >= ~7 V on B+, so the
# power supply stays at V_SUPPLY and the Arduino's PWM duty sets the average Peltier voltage.
V_SUPPLY = 8.0        # Fixed power supply voltage (V)
V_COOL_MAX = 6.0      # Max average Peltier voltage while cooling (V)
V_HEAT_MAX = 3.0      # Max average Peltier voltage while heating; heating is much stronger than cooling (V)
V_MIN = 0.05          # PID outputs smaller than this are treated as 0 V (dead band around the target)
CURRENT_LIMIT = 2.0   # Power supply current limit (A)
# Bridge command that cools the Peltier: 'f' (RPWM, pin 5) or 'b' (LPWM, pin 6). Swap if it heats instead.
COOL_CMD = 'f'
HEAT_CMD = 'b' if COOL_CMD == 'f' else 'f'

# PID parameters
Kp, Ki, Kd = 1.5, 0.08, 0.05

def signal_handler(signum, frame):
    """Handle keyboard interrupts and system signals"""
    print("\nSignal received. Saving data and shutting down...")
    raise KeyboardInterrupt

def control_power_supply_with_temp_monitoring(target=TARGET_TEMPERATURE, stop_event=None, on_reading=None):
    """Heat or cool the Peltier to `target` and hold it there until `stop_event` is set (or Ctrl+C).

    `on_reading`, if given, is called with each logged data row (used by the UI).
    Returns None on a normal stop, or the error message if the run stopped on an error.
    """
    if not TARGET_RANGE[0] <= target <= TARGET_RANGE[1]:
        raise ValueError(f"Target {target}°C is outside the allowed range {TARGET_RANGE}")
    stop_event = stop_event or threading.Event()
    # Initialize signal handlers for graceful shutdown (only possible on the main thread)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_folder = f'measurements_{timestamp}'
    os.makedirs(output_folder, exist_ok=True)

    temp_logger = None
    ps = None
    rm = None
    data = []

    integral = 0
    prev_error = 0
    voltage = 0.0       # Signed PID output: + cooling, - heating
    direction = 'o'     # Current bridge command
    duty = 0            # Current PWM duty (0-255)
    start_error = None
    failed_reads = 0

    def apply_voltage(voltage):
        """Set bridge direction and PWM duty for a signed average Peltier voltage (+ cooling, - heating)"""
        nonlocal direction, duty
        duty = min(255, round(abs(voltage) / V_SUPPLY * 255)) if abs(voltage) >= V_MIN else 0
        if duty:
            direction = COOL_CMD if voltage > 0 else HEAT_CMD
        if direction != 'o':
            temp_logger.set_bridge(direction, duty)

    try:
        port = next((port.device for port in serial.tools.list_ports.comports() if 'ch340' in port.description.lower()), None)   # write the name of your arduino device name
        print(f"Arduino device port: {port}" if port else "No CH340 device found")
        temp_logger = PT100TempLogger(port)
        temp_logger.set_bridge('o')  # Also checks that the H-bridge firmware is on the Arduino
        # Check the temperature sensor before powering anything
        for _ in range(3):
            if temp_logger.read_temperature() is not None:
                break
            time.sleep(1)
        else:
            raise RuntimeError("No valid Pt100 reading; check the Pt100 / MAX31865 wiring (pins 10-13, power)")

        port2 = next((p.device for p in serial.tools.list_ports.comports() if 'PL2303GT' in str(p.description)), None)   # write the name of your power supply device name
        print(f"Power supply port: {port2}" if port2 else "No power supply device found")
        try:
            rm = pyvisa.ResourceManager()
        except (OSError, ValueError):
            rm = pyvisa.ResourceManager('@py')  # No NI-VISA installed; use pyvisa-py backend
        # pyvisa-py doesn't accept "COM30"-style aliases, so use the ASRL resource name
        serial_id = port2[3:] if port2.upper().startswith('COM') else port2
        ps = rm.open_resource(f'ASRL{serial_id}::INSTR')

        ps.write('*RST')
        ps.write('SYST:REM')
        ps.write(f'CURR {CURRENT_LIMIT}')
        ps.write(f'VOLT {V_SUPPLY}')
        ps.write('OUTP ON')

        print(f"Target temperature: {target}°C")
        start_time = time.time()

        while not stop_event.is_set():
            temperature = temp_logger.read_temperature()

            if temperature is None:  # On a failed reading, hold the previous voltage
                failed_reads += 1
                if failed_reads >= 5:
                    raise RuntimeError("5 temperature readings failed in a row; check the Pt100 / MAX31865 wiring")
            else:
                failed_reads = 0
                if not SAFE_RANGE[0] <= temperature <= SAFE_RANGE[1]:
                    raise RuntimeError(f"Temperature {temperature:.2f}°C is outside the safe range {SAFE_RANGE}")
                error = abs(temperature - target)
                if start_error is None:
                    start_error = error
                elif error > start_error + 3:
                    raise RuntimeError("Temperature is moving away from the target; check COOL_CMD / Peltier polarity")
                voltage, integral, prev_error = pid_control(
                    target, temperature, Kp, Ki, Kd, integral, prev_error, V_HEAT_MAX, V_COOL_MAX
                )
                apply_voltage(voltage)

            sign = -1 if direction == HEAT_CMD else 1  # Log heating as negative voltage/current
            supply_voltage = float(ps.query('MEAS:VOLT?'))
            actual_voltage = sign * supply_voltage * duty / 255  # Average voltage across the Peltier
            actual_current = sign * float(ps.query('MEAS:CURR?'))
            timestamp = datetime.now()

            data.append({
                'Timestamp': timestamp,
                'Target_Temperature': target,
                'Set_Voltage': voltage if abs(voltage) >= V_MIN else 0.0,
                'PWM_Duty_Percent': 100 * duty / 255,
                'Supply_Voltage': supply_voltage,
                'Measured_Voltage': actual_voltage,
                'Measured_Current': actual_current,
                'Temperature': temperature
            })
            if on_reading:
                on_reading(data[-1])

            print(f'\nElapsed time: {time.time() - start_time:.1f}s')
            print(f'Time: {timestamp.strftime("%H:%M:%S")}')
            state = 'off' if abs(voltage) < V_MIN else ('cooling' if voltage > 0 else 'heating')
            print(f'Set Voltage: {voltage:+.3f}V ({state}, PWM {100 * duty / 255:.0f}% of {supply_voltage:.2f}V)')
            print(f'Measured Voltage: {actual_voltage:+.3f}V')
            print(f'Measured Current: {actual_current:+.3f}A')
            print(f'Temperature: {temperature:.2f}°C (target {target}°C)' if temperature is not None else 'Temperature: Reading Error')
            print('-' * 50)

            stop_event.wait(1)

        # Normal stop - save data and create plots
        excel_file, plot_file = save_data(data, output_folder)
        print(f"\nMeasurement stopped.")
        print(f"Data saved to: {excel_file}")
        print(f"Plots saved to: {plot_file}")
        return None

    except KeyboardInterrupt:
        print("\nProgram stopped by user.")
        excel_file, plot_file = save_data(data, output_folder)
        print(f"Data saved to: {excel_file}")
        print(f"Plots saved to: {plot_file}")
        return None

    except Exception as e:
        print(f"An error occurred: {str(e)}")
        excel_file, plot_file = save_data(data, output_folder, error=True)
        print(f"Partial data saved to: {excel_file}")
        print(f"Partial plots saved to: {plot_file}")
        return str(e)

    finally:
        # Cleanup resources: supply output off first, then the bridge
        if ps:
            try:
                ps.write('OUTP OFF')
                ps.write('VOLT 0')
                ps.close()
            except:
                pass

        if rm:
            try:
                rm.close()
            except:
                pass

        if temp_logger:
            try:
                temp_logger.set_bridge('o')
            except:
                pass
            try:
                del temp_logger
            except:
                pass

if __name__ == "__main__":
    control_power_supply_with_temp_monitoring()
