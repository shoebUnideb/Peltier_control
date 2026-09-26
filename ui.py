import matplotlib
matplotlib.use('Agg')  # The controller saves its plots from a worker thread; keep pyplot off Tk
import queue
import threading
import tkinter as tk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from main import control_power_supply_with_temp_monitoring, TARGET_TEMPERATURE, TARGET_RANGE

LINE = '#2a78d6'
MUTED = '#898781'
INK = '#52514e'


class ControllerUI:
    def __init__(self, root):
        self.root = root
        self.readings = queue.Queue()
        self.stop_event = None
        self.worker = None
        self.error = None

        root.title('Peltier PID Controller')
        bar = tk.Frame(root)
        bar.pack(fill='x', padx=8, pady=6)
        tk.Label(bar, text=f'Target (°C, {TARGET_RANGE[0]:g}–{TARGET_RANGE[1]:g}):').pack(side='left')
        self.target_var = tk.StringVar(value=str(TARGET_TEMPERATURE))
        self.target_entry = tk.Entry(bar, textvariable=self.target_var, width=7)
        self.target_entry.pack(side='left', padx=(4, 12))
        self.start_btn = tk.Button(bar, text='Start', width=10, command=self.start)
        self.start_btn.pack(side='left')
        self.stop_btn = tk.Button(bar, text='Stop', width=10, command=self.stop, state='disabled')
        self.stop_btn.pack(side='left', padx=6)
        self.status = tk.Label(bar, text='Idle')
        self.status.pack(side='left', padx=12)

        fig = Figure(figsize=(14, 4.5), layout='constrained')
        self.axes = fig.subplots(1, 3)
        titles = [('Temperature vs Time', 'Time (s)', 'Temperature (°C)'),
                  ('Voltage vs Time', 'Time (s)', 'Voltage (V)   + cooling / − heating'),
                  ('Temperature vs Voltage', 'Voltage (V)   + cooling / − heating', 'Temperature (°C)')]
        self.lines = []
        for ax, (title, xlabel, ylabel) in zip(self.axes, titles):
            ax.set_title(title, loc='left', color=INK)
            ax.set_xlabel(xlabel, color=INK)
            ax.set_ylabel(ylabel, color=INK)
            ax.grid(True, color='#e1e0dc', linewidth=0.8)
            ax.tick_params(colors=MUTED)
            for side in ('top', 'right'):
                ax.spines[side].set_visible(False)
            self.lines.append(ax.plot([], [], color=LINE, linewidth=2)[0])
        self.target_line = self.axes[0].axhline(TARGET_TEMPERATURE, color=MUTED, linestyle='--', linewidth=1)
        self.axes[1].axhline(0, color=MUTED, linewidth=1)
        self.axes[2].axvline(0, color=MUTED, linewidth=1)
        self.canvas = FigureCanvasTkAgg(fig, master=root)
        self.canvas.get_tk_widget().pack(fill='both', expand=True)

        root.protocol('WM_DELETE_WINDOW', self.close)
        self.poll()

    def start(self):
        try:
            target = float(self.target_var.get())
        except ValueError:
            target = None
        if target is None or not TARGET_RANGE[0] <= target <= TARGET_RANGE[1]:
            self.status.config(text=f'Enter a target between {TARGET_RANGE[0]:g} and {TARGET_RANGE[1]:g} °C')
            return
        self.target_line.set_ydata([target, target])
        self.canvas.draw_idle()

        self.t, self.temp, self.volt = [], [], []
        self.t0 = None
        self.error = None
        self.stop_event = threading.Event()

        def run():
            self.error = control_power_supply_with_temp_monitoring(target, self.stop_event, self.readings.put)

        self.worker = threading.Thread(target=run)
        self.worker.start()
        self.target_entry.config(state='disabled')
        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        self.status.config(text=f'Starting, target {target:g} °C...')

    def stop(self):
        self.stop_event.set()
        self.stop_btn.config(state='disabled')
        self.status.config(text='Stopping: power supply off, saving data...')

    def poll(self):
        new_data = False
        while not self.readings.empty():
            row = self.readings.get()
            self.t0 = self.t0 or row['Timestamp']
            self.t.append((row['Timestamp'] - self.t0).total_seconds())
            self.temp.append(float('nan') if row['Temperature'] is None else row['Temperature'])
            self.volt.append(row['Measured_Voltage'])
            new_data = True
        if new_data:
            for line, (x, y) in zip(self.lines, [(self.t, self.temp), (self.t, self.volt), (self.volt, self.temp)]):
                line.set_data(x, y)
            for ax in self.axes:
                ax.relim()
                ax.autoscale_view()
            self.canvas.draw_idle()
            mode = 'off' if abs(self.volt[-1]) < 0.05 else ('cooling' if self.volt[-1] > 0 else 'heating')
            self.status.config(text=f'Running  |  t = {self.t[-1]:.0f} s   T = {self.temp[-1]:.2f} °C   '
                                    f'V = {self.volt[-1]:+.3f} V ({mode})')
        if self.worker and not self.worker.is_alive():
            self.worker = None
            self.target_entry.config(state='normal')
            self.start_btn.config(state='normal')
            self.stop_btn.config(state='disabled')
            self.status.config(text=f'Stopped with error: {self.error}' if self.error
                               else 'Stopped: power supply off, data saved')
        self.root.after(300, self.poll)

    def close(self):
        # The worker thread keeps running until it has turned the supply output off and saved data
        if self.stop_event:
            self.stop_event.set()
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    ControllerUI(root)
    root.mainloop()
