import pyvisa
import time
import os
from datetime import datetime
import signal
import threading
from pid_utils import PT100TempLogger, DewPointTarget, dew_point, save_data, pid_control
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

# PID parameters, per second of elapsed time (the loop runs about every 1.3 s). The PID output is an
# "effort" in cooling volts: + cooling, - heating.
Kp, Ki, Kd = 1.5, 0.06, 0.065
# Heating effort is scaled by this before it drives the Peltier: an effort of -E drives -E * HEAT_SCALE
# volts of heating. 1.0 = same response as cooling. Lower it (e.g. 0.5) if heating oscillates.
HEAT_SCALE = 1.0
# Feedforward (cooling only): the integral starts at K_FF * (ambient - target) instead of 0, so the PID
# doesn't have to build up the holding voltage from nothing. The 2026-09-24 runs needed ~0.85 V/°C
# (3.9 V held 19.5 °C at ~24 °C); starting at about half of that settled fastest with the least
# overshoot in simulation. Heating has no feedforward until its holding voltage has been measured.
# Set to 0 to disable.
K_FF = 0.5  # V/°C
# While the temperature is more than this many °C from the target and still moving toward it, the
# integral pauses: integrating the large errors of the approach caused the overshoot.
INTEGRAL_BAND = 1.0

# Dew point mode: the target follows the dew point of the room air (from the DHT22). The plate first cools at
# full speed to DEW_APPROACH_OFFSET above the dew point, then the target ramps down to DEW_HOLD_OFFSET above it
# over DEW_SETTLE_TIME seconds and holds there until the run is stopped.
DEW_APPROACH_OFFSET = 2.5  # °C above the dew point for the fast approach
DEW_SETTLE_TIME = 10.0     # Seconds to ramp the target from the approach offset to the hold offset
DEW_HOLD_OFFSET = 0.0      # °C above the dew point to hold (0 = at the dew point)
DEW_SMOOTHING = 0.1        # Weight of each new dew point reading (~10 readings to follow a change)
DEW_APPROACH_BAND = 0.3    # The settle phase starts once the plate is within this of the approach target (°C)
# Optional sine wave around the hold point instead of holding still: one cycle per DEW_SINE_PERIOD seconds,
# DEW_SINE_AMPLITUDE °C above and below. A period of 0 means no wave. The plate takes ~10-20 s to respond, so
# much shorter periods come out smaller than asked and lag behind the target.
DEW_SINE_PERIOD = 0.0      # s
DEW_SINE_AMPLITUDE = 1.0   # °C
# On the sine wave the controller aims at the target this many seconds ahead, because the plate lags ~5-8 s behind
# (measured 2026-10-22 with a 120 s period). 0 = aim at the current target.
DEW_SINE_LEAD = 6.0        # s

def signal_handler(signum, frame):
    """Handle keyboard interrupts and system signals"""
    print("\nSignal received. Saving data and shutting down...")
    raise KeyboardInterrupt

def find_arduino_port():
    """Auto-detect the Arduino's CH340 USB-serial adapter; None if not found"""
    return next((p.device for p in serial.tools.list_ports.comports() if 'ch340' in p.description.lower()), None)

def find_power_supply_port():
    """Auto-detect the power supply's PL2303 USB-serial adapter; None if not found"""
    return next((p.device for p in serial.tools.list_ports.comports() if 'PL2303GT' in str(p.description)), None)

def open_power_supply(ps_port):
    """Open the SCPI power supply on `ps_port` (a COM device string) and return (resource_manager, resource)"""
    if not ps_port:
        raise RuntimeError("No power supply device found; check the USB cable / driver")
    try:
        rm = pyvisa.ResourceManager()
    except (OSError, ValueError):
        rm = pyvisa.ResourceManager('@py')  # No NI-VISA installed; use pyvisa-py backend
    # pyvisa-py doesn't accept "COM30"-style aliases, so use the ASRL resource name
    serial_id = ps_port[3:] if ps_port.upper().startswith('COM') else ps_port
    return rm, rm.open_resource(f'ASRL{serial_id}::INSTR')

def preflight_check(arduino_port=None, ps_port=None):
    """Check the Arduino and power supply connections without energizing the Peltier.

    `arduino_port`/`ps_port` override auto-detection (e.g. from a UI port picker). Returns a list of
    dicts {'name', 'ok', 'message'}: `ok` is True (fine), False (needs fixing) or None (a soft warning,
    e.g. the DHT22 isn't essential). Used by the UI to warn about wiring/connection problems up front;
    the run loop below does the same checks and will raise clearly if something is actually wrong.
    """
    checks = []
    temp_logger = None

    port = arduino_port or find_arduino_port()
    checks.append({'name': 'Arduino port', 'ok': port is not None,
                   'message': f'Found on {port}' if port else 'No CH340 device found; check the USB cable / driver'})
    if port:
        try:
            temp_logger = PT100TempLogger(port)
            temp_logger.set_bridge('o')
            checks.append({'name': 'H-bridge firmware', 'ok': True, 'message': "Acknowledged the bridge-off command"})
        except Exception as e:
            checks.append({'name': 'H-bridge firmware', 'ok': False,
                           'message': f"{e} — is firmware/peltier_controller uploaded to the Arduino?"})
        if temp_logger:
            plate_temperature = None
            for _ in range(3):
                plate_temperature = temp_logger.read_temperature()
                if plate_temperature is not None:
                    break
                time.sleep(0.3)
            checks.append({'name': 'Pt100 sensor', 'ok': plate_temperature is not None,
                           'message': f'{plate_temperature:.2f}°C' if plate_temperature is not None
                                      else 'No valid reading; check the Pt100 / MAX31865 wiring (pins 10-13, power)'})
            ambient = temp_logger.read_ambient_temperature()
            checks.append({'name': 'DHT22 (ambient)', 'ok': None if ambient is None else True,
                           'message': f'{ambient:.1f}°C' if ambient is not None
                                      else "No reading; feedforward will use the plate temperature instead"})
            try:
                temp_logger.set_bridge('o')
            except Exception:
                pass
            del temp_logger

    port2 = ps_port or find_power_supply_port()
    checks.append({'name': 'Power supply port', 'ok': port2 is not None,
                   'message': f'Found on {port2}' if port2 else 'No power supply device found; check the USB cable / driver'})
    if port2:
        rm = ps = None
        try:
            rm, ps = open_power_supply(port2)
            voltage = float(ps.query('MEAS:VOLT?'))  # Non-invasive: reads the output without touching it
            voltage = 0.0 if abs(voltage) < 0.005 else voltage  # Avoid a confusing "-0.00 V" from ADC noise
            checks.append({'name': 'Power supply comms', 'ok': True, 'message': f'Responding, reads {voltage:.2f} V'})
        except Exception as e:
            checks.append({'name': 'Power supply comms', 'ok': False, 'message': str(e)})
        finally:
            for obj in (ps, rm):
                try:
                    obj and obj.close()
                except Exception:
                    pass

    return checks

def control_power_supply_with_temp_monitoring(target=TARGET_TEMPERATURE, stop_event=None, on_reading=None,
                                              arduino_port=None, ps_port=None, dew=False,
                                              dew_hold_offset=DEW_HOLD_OFFSET, sine_period=DEW_SINE_PERIOD,
                                              sine_amplitude=DEW_SINE_AMPLITUDE):
    """Heat or cool the Peltier to `target` and hold it there until `stop_event` is set (or Ctrl+C).

    With `dew=True`, `target` is ignored and the plate is brought to `dew_hold_offset` °C above the dew point
    of the room air instead (see DewPointTarget), following it as the humidity changes. With a `sine_period` (s)
    the target then swings `sine_amplitude` °C around that point instead of holding still.

    `on_reading`, if given, is called with each logged data row (used by the UI). `arduino_port`/`ps_port`
    override auto-detection.
    Returns (error, output_folder): `error` is None on a normal stop or the message if the run stopped on
    an error; `output_folder` is the measurements_<timestamp> folder the data and plots were saved to.
    """
    if not dew and not TARGET_RANGE[0] <= target <= TARGET_RANGE[1]:
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

    effort_min, effort_max = -V_HEAT_MAX / HEAT_SCALE, V_COOL_MAX  # PID output limits (cooling volts)
    integral = 0.0
    prev_temp = None
    prev_time = None
    ambient = None
    voltage = 0.0       # Signed Peltier voltage: + cooling, - heating
    direction = 'o'     # Current bridge command
    duty = 0            # Current PWM duty (0-255)
    start_error = None
    failed_reads = 0
    dew_target = None   # DewPointTarget in dew point mode
    clamped = False     # Whether the dew point target was clamped to TARGET_RANGE

    def apply_voltage(voltage):
        """Set bridge direction and PWM duty for a signed average Peltier voltage (+ cooling, - heating)"""
        nonlocal direction, duty
        duty = min(255, round(abs(voltage) / V_SUPPLY * 255)) if abs(voltage) >= V_MIN else 0
        if duty:
            direction = COOL_CMD if voltage > 0 else HEAT_CMD
        if direction != 'o':
            temp_logger.set_bridge(direction, duty)

    try:
        port = arduino_port or find_arduino_port()
        print(f"Arduino device port: {port}" if port else "No CH340 device found")
        temp_logger = PT100TempLogger(port)
        temp_logger.set_bridge('o')  # Also checks that the H-bridge firmware is on the Arduino
        # Check the temperature sensor before powering anything
        for _ in range(3):
            plate_temperature = temp_logger.read_temperature()
            if plate_temperature is not None:
                break
            time.sleep(1)
        else:
            raise RuntimeError("No valid Pt100 reading; check the Pt100 / MAX31865 wiring (pins 10-13, power)")

        # The DHT22 needs ~2 s between reads, so retry slowly
        for attempt in range(3):
            _, humidity, ambient = temp_logger.read_all()
            if humidity is not None and ambient is not None:
                break
            if attempt < 2:
                time.sleep(2.5)
        if dew:
            if humidity is None or ambient is None:
                raise RuntimeError("Dew point mode needs the DHT22 (humidity) but it gives no reading; check its wiring")
            dew_target = DewPointTarget(dew_point(ambient, humidity), DEW_APPROACH_OFFSET, DEW_SETTLE_TIME,
                                        dew_hold_offset, DEW_SMOOTHING, DEW_APPROACH_BAND, sine_period,
                                        sine_amplitude, DEW_SINE_LEAD)
            target = dew_target.update(plate_temperature, None, None, time.monotonic())
            print(f"Dew point: {dew_target.dew:.2f}°C (air {ambient:.1f}°C, RH {humidity:.1f}%); "
                  f"cooling to {target:.2f}°C, then settling to {dew_target.dew + dew_hold_offset:.2f}°C"
                  + (f", then a sine wave of ±{sine_amplitude:g}°C with a {sine_period:g} s period"
                     if sine_period > 0 and sine_amplitude else ''))
            if dew_target.dew + dew_hold_offset < TARGET_RANGE[0]:
                print(f"Warning: the dew point target is below {TARGET_RANGE[0]}°C; the target is clamped there")

        # Feedforward: preload the integral with the effort expected to hold the (final) target
        source = 'DHT22'
        if ambient is None:  # No DHT22 reading; the plate is at ambient if it has been idle
            ambient, source = plate_temperature, 'plate, DHT22 unavailable'
        hold_target = dew_target.dew + dew_hold_offset if dew else target
        integral = min(max(0.0, K_FF * (ambient - hold_target)), effort_max)
        print(f"Ambient: {ambient:.2f}°C ({source}); feedforward effort {integral:+.2f}V")

        port2 = ps_port or find_power_supply_port()
        print(f"Power supply port: {port2}" if port2 else "No power supply device found")
        rm, ps = open_power_supply(port2)

        ps.write('*RST')
        ps.write('SYST:REM')
        ps.write(f'CURR {CURRENT_LIMIT}')
        ps.write(f'VOLT {V_SUPPLY}')
        ps.write('OUTP ON')

        print(f"Target temperature: {'dew point' if dew else f'{target}°C'}")
        start_time = time.time()

        while not stop_event.is_set():
            temperature, humidity, air_temperature = temp_logger.read_all()
            current_dew = dew_point(air_temperature, humidity) if humidity and air_temperature is not None else None
            control_target = target  # What the PID aims at; differs from `target` only on the sine wave
            if dew_target:
                phase = dew_target.phase
                target = dew_target.update(temperature, humidity, air_temperature, time.monotonic())
                control_target = dew_target.control_target(time.monotonic())
                if dew_target.phase != phase:
                    print(f"\nPhase: {phase} -> {dew_target.phase} (target {target:.2f}°C)")
                if not TARGET_RANGE[0] <= min(target, control_target) <= max(target, control_target) <= TARGET_RANGE[1]:
                    target, control_target = (min(max(t, TARGET_RANGE[0]), TARGET_RANGE[1])
                                              for t in (target, control_target))
                    if not clamped:
                        print(f"Warning: target clamped to {target}°C (allowed range {TARGET_RANGE})")
                    clamped = True

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
                now = time.monotonic()
                dt = now - prev_time if prev_time is not None else 0.0
                effort, integral = pid_control(
                    control_target, temperature, prev_temp, dt, Kp, Ki, Kd, integral, effort_min, effort_max,
                    INTEGRAL_BAND
                )
                prev_temp, prev_time = temperature, now
                voltage = effort if effort >= 0 else effort * HEAT_SCALE
                apply_voltage(voltage)

            sign = -1 if direction == HEAT_CMD else 1  # Log heating as negative voltage/current
            supply_voltage = float(ps.query('MEAS:VOLT?'))
            actual_voltage = sign * supply_voltage * duty / 255  # Average voltage across the Peltier
            actual_current = sign * float(ps.query('MEAS:CURR?'))
            timestamp = datetime.now()

            data.append({
                'Timestamp': timestamp,
                'Target_Temperature': target,
                'Ambient_Temperature': ambient,
                'Air_Temperature': air_temperature,
                'Humidity': humidity,
                'Dew_Point': dew_target.dew if dew_target else current_dew,
                'Phase': dew_target.phase if dew_target else 'fixed',
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
            if current_dew is not None:
                print(f'Air: {air_temperature:.1f}°C, RH {humidity:.1f}%, dew point '
                      f'{dew_target.dew if dew_target else current_dew:.2f}°C'
                      + (f', phase {dew_target.phase}' if dew_target else ''))
            print(f'Temperature: {temperature:.2f}°C (target {target:.2f}°C)' if temperature is not None else 'Temperature: Reading Error')
            print('-' * 50)

            stop_event.wait(1)

        # Normal stop - save data and create plots
        excel_file, plot_file = save_data(data, output_folder)
        print(f"\nMeasurement stopped.")
        print(f"Data saved to: {excel_file}")
        print(f"Plots saved to: {plot_file}")
        return None, output_folder

    except KeyboardInterrupt:
        print("\nProgram stopped by user.")
        excel_file, plot_file = save_data(data, output_folder)
        print(f"Data saved to: {excel_file}")
        print(f"Plots saved to: {plot_file}")
        return None, output_folder

    except Exception as e:
        print(f"An error occurred: {str(e)}")
        excel_file, plot_file = save_data(data, output_folder, error=True)
        print(f"Partial data saved to: {excel_file}")
        print(f"Partial plots saved to: {plot_file}")
        return str(e), output_folder

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

STOP_FILE = 'STOP'  # Creating this file in the working directory stops a run cleanly (used by run.sh stop)

def watch_for_stop(stop_event, duration=None):
    """Set `stop_event` once `duration` seconds have passed or STOP_FILE appears"""
    deadline = time.monotonic() + duration if duration else None
    while not stop_event.wait(1):
        if os.path.exists(STOP_FILE):
            print(f"\n{STOP_FILE} file found, stopping...")
            stop_event.set()
        elif deadline and time.monotonic() >= deadline:
            print(f"\nDuration of {duration / 60:g} min reached, stopping...")
            stop_event.set()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Heat or cool the Peltier to a target temperature and hold it")
    parser.add_argument('--target', type=float, default=TARGET_TEMPERATURE, help="Target temperature (°C)")
    parser.add_argument('--dew', action='store_true',
                        help="Dew point mode: cool to the dew point of the room air and hold it (ignores --target)")
    parser.add_argument('--dew-offset', type=float, default=DEW_HOLD_OFFSET,
                        help="Dew point mode: hold this many °C above the dew point (default %(default)s)")
    parser.add_argument('--sine-period', type=float, default=DEW_SINE_PERIOD,
                        help="Dew point mode: swing the target in a sine wave around the hold point with this "
                             "period in seconds (default %(default)s = hold still)")
    parser.add_argument('--sine-amplitude', type=float, default=DEW_SINE_AMPLITUDE,
                        help="Dew point mode: sine wave amplitude in °C (default %(default)s)")
    parser.add_argument('--duration', type=float, default=None,
                        help="Stop after this many minutes (default: run until Ctrl+C or a STOP file)")
    args = parser.parse_args()

    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)  # Left over from an earlier run
    stop_event = threading.Event()
    duration = args.duration * 60 if args.duration else None
    threading.Thread(target=watch_for_stop, args=(stop_event, duration), daemon=True).start()
    control_power_supply_with_temp_monitoring(target=args.target, stop_event=stop_event, dew=args.dew,
                                              dew_hold_offset=args.dew_offset, sine_period=args.sine_period,
                                              sine_amplitude=args.sine_amplitude)
