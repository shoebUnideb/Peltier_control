"""Live plot of a running experiment, fed with main.py's console output on stdin.

Used by `./run.sh plot`, which streams the lab PC's console.log over SSH. Also replays a saved log:
    python3 live_plot.py < logs/measurements_<timestamp>/console.log
"""
import queue
import sys
import threading

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

from log_parser import ConsoleParser

lines = queue.Queue()
parser = ConsoleParser()
stream_ended = threading.Event()


def read_stdin():
    for line in sys.stdin:
        lines.put(line)
    stream_ended.set()


fig, (ax_temp, ax_volt, ax_curr) = plt.subplots(3, 1, sharex=True, figsize=(10, 8), layout='constrained')
fig.canvas.manager.set_window_title('Peltier live')
line_temp, = ax_temp.plot([], [], 'b-', label='Plate')
line_target, = ax_temp.plot([], [], 'k--', label='Target')
line_dew, = ax_temp.plot([], [], 'c:', label='Dew point')
line_set, = ax_volt.plot([], [], 'r-', label='Set')
line_volt, = ax_volt.plot([], [], 'g-', label='Measured')
line_curr, = ax_curr.plot([], [], 'm-')
ax_temp.set_ylabel('Temperature (°C)')
ax_volt.set_ylabel('Peltier voltage (V)\n+ cooling, - heating')
ax_curr.set_ylabel('Current (A)')
ax_curr.set_xlabel('Time (s)')
for ax in (ax_temp, ax_volt, ax_curr):
    ax.grid(True)
ax_temp.legend(loc='upper right')
ax_volt.legend(loc='upper right')


def update(_):
    while not lines.empty():
        parser.feed(lines.get())
    rows = parser.rows
    t = [r['time'] for r in rows]
    for line, key in ((line_temp, 'temperature'), (line_target, 'target'), (line_dew, 'dew'),
                      (line_set, 'set_voltage'), (line_volt, 'voltage'), (line_curr, 'current')):
        line.set_data(t, [r[key] for r in rows])
    for ax in (ax_temp, ax_volt, ax_curr):
        ax.relim()
        ax.autoscale_view()
    status = parser.end or ('Stream ended (connection closed?)' if stream_ended.is_set() and lines.empty() else '')
    if rows:
        r = rows[-1]
        latest = (f"{r['time']:.0f} s   {r['temperature']:.2f} °C (target {r['target']:.2f} °C)   "
                  f"{r['voltage']:+.2f} V   {r['current']:+.3f} A")
        if r['dew'] == r['dew']:  # not nan
            latest += f"   dew point {r['dew']:.2f} °C" + (f" ({r['phase']})" if r['phase'] else '')
        fig.suptitle(f"{latest}\n{status}" if status else latest)
    else:
        fig.suptitle(status or 'Waiting for data...')
    return ()


threading.Thread(target=read_stdin, daemon=True).start()
animation = FuncAnimation(fig, update, interval=1000, cache_frame_data=False)
plt.show()
