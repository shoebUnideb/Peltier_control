# 2026-09-24: H-bridge PID runs

Bidirectional control through a BTS7960 H-bridge. The E3632A is fixed at 8 V and the Arduino's
PWM duty on RPWM/LPWM sets the average Peltier voltage (the BTS7960 needs >= ~7 V to switch fully on).
Target 19.5 °C in all runs, started from the UI (`python ui.py`). Firmware: `firmware/peltier_controller`.

| Folder | Duration | Kp, Ki, Kd | Start temp | Result |
|---|---|---|---|---|
| `measurements_20260924_160741` | 7 min | 0.65, 0.01, 0.05 | 24.9 °C | Too slow: still 19.84 °C at 7 min |
| `measurements_20260924_161524` | 5 min | 1.5, 0.04, 0.05 | 24.1 °C | Last 2 min mean 19.59 °C (19.54–19.67) |
| `measurements_20260924_162047` | 5 min | 1.5, 0.08, 0.05 | 24.0 °C | **Last 2 min mean 19.52 °C (19.47–19.54)**; settled in ~2.5 min |

The last row's gains are the ones now in `main.py`.

Each folder has the data (`power_supply_temp_log.xlsx`), the saved plots, UI screenshots (`ui_*.png`)
and the terminal output (`console.log`).

Observations:
- The voltage needed to hold 19.5 °C rises during a run (3.8 V → 4.0 V over 5 min in the last run) as
  the heat sink warms. The cooling limit is `V_COOL_MAX = 6.0` V.
- Heating under PID control was not exercised on hardware in these runs (all cooling). Manual `b`
  (heating) commands were tested and work.
