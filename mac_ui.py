"""Control the Peltier on the lab PC from this machine: start/stop runs, watch them live, fetch the results.

    python3 mac_ui.py

Everything goes through run.sh (SSH to the "labpc" host), so the lab PC does the actual control and keeps running
if this window is closed or the connection drops. Needs Python with tkinter and matplotlib on this machine.
"""
import os
import queue
import signal
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from log_parser import ConsoleParser

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_SH = os.path.join(HERE, 'run.sh')
TARGET_RANGE = (5.0, 40.0)   # Same as main.py
MAX_COOL_PWM = 75            # main.py's V_COOL_MAX / V_SUPPLY (6 V of 8 V): cooling is at its limit here
STATUS_POLL_MS = 20000       # How often to ask the lab PC whether a run is going

BG = '#fcfcfb'
PANEL = '#f3f2ef'
INK = '#0b0b0b'
INK2 = '#52514e'
MUTED = '#898781'
BLUE = '#2a78d6'
CYAN = '#1aa3b8'
ORANGE = '#eb6834'
GREEN = '#0ca30c'
RED = '#c4362f'
AMBER = '#b9790a'


class Stat(tk.Frame):
    """A small labeled readout"""
    def __init__(self, master, label):
        super().__init__(master, bg=PANEL, padx=10)
        tk.Label(self, text=label, font=('Helvetica', 10), fg=MUTED, bg=PANEL).pack(anchor='w')
        self.value = tk.Label(self, text='—', font=('Helvetica', 16, 'bold'), fg=INK, bg=PANEL)
        self.value.pack(anchor='w')

    def set(self, text, color=INK):
        self.value.config(text=text, fg=color)


class MacUI:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()   # (kind, payload) from worker threads, handled on the Tk thread
        self.parser = ConsoleParser()
        self.stream = None            # Popen of `run.sh stream` while following a run
        self.busy = None              # Name of the run.sh command in progress, if any
        self.running = False          # Whether the lab PC is running an experiment
        self.dirty = False            # New readings since the last redraw

        root.title('Peltier control (lab PC)')
        root.configure(bg=BG)
        root.geometry('1200x940')
        root.minsize(1000, 750)
        self.build_header()
        self.build_controls()
        self.build_stats()
        self.build_body()
        self.update_buttons()

        root.protocol('WM_DELETE_WINDOW', self.close)
        self.poll_events()
        self.redraw_loop()
        self.refresh_status()

    # ------------------------------------------------------------------ layout
    def build_header(self):
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill='x', padx=12, pady=(10, 4))
        tk.Label(bar, text='Peltier control', font=('Helvetica', 18, 'bold'), bg=BG, fg=INK).pack(side='left')
        self.dot = tk.Canvas(bar, width=12, height=12, bg=BG, highlightthickness=0)
        self.dot_oval = self.dot.create_oval(1, 1, 11, 11, fill=MUTED, outline='')
        self.dot.pack(side='left', padx=(16, 6))
        self.status_label = tk.Label(bar, text='Connecting to the lab PC…', bg=BG, fg=INK2, font=('Helvetica', 13))
        self.status_label.pack(side='left')
        ttk.Button(bar, text='Open logs folder', command=lambda: subprocess.run(['open', os.path.join(HERE, 'logs')])
                   ).pack(side='right')
        self.fetch_btn = ttk.Button(bar, text='Fetch last run', command=self.fetch)
        self.fetch_btn.pack(side='right', padx=6)
        self.check_btn = ttk.Button(bar, text='Check hardware', command=self.check)
        self.check_btn.pack(side='right')

    def set_status(self, text, color):
        self.status_label.config(text=text)
        self.dot.itemconfig(self.dot_oval, fill=color)

    def build_controls(self):
        panel = tk.Frame(self.root, bg=PANEL)
        panel.pack(fill='x', padx=12, pady=6)
        row = tk.Frame(panel, bg=PANEL)
        row.pack(fill='x', padx=10, pady=10)

        self.mode = tk.StringVar(value='dew')
        tk.Label(row, text='Mode:', bg=PANEL, fg=INK2).pack(side='left')
        ttk.Radiobutton(row, text='Dew point', variable=self.mode, value='dew',
                        command=self.update_buttons).pack(side='left', padx=(6, 0))
        ttk.Radiobutton(row, text='Fixed target', variable=self.mode, value='fixed',
                        command=self.update_buttons).pack(side='left', padx=(6, 16))

        tk.Label(row, text='Target (°C):', bg=PANEL, fg=INK2).pack(side='left')
        self.target_var = tk.StringVar(value='19.5')
        self.target_spin = ttk.Spinbox(row, from_=TARGET_RANGE[0], to=TARGET_RANGE[1], increment=0.5,
                                       textvariable=self.target_var, width=6)
        self.target_spin.pack(side='left', padx=(4, 16))

        tk.Label(row, text='Hold above dew point (°C):', bg=PANEL, fg=INK2).pack(side='left')
        self.offset_var = tk.StringVar(value='0')
        self.offset_spin = ttk.Spinbox(row, from_=-2, to=10, increment=0.5, textvariable=self.offset_var, width=5)
        self.offset_spin.pack(side='left', padx=(4, 16))

        tk.Label(row, text='Duration (min, 0 = until stopped):', bg=PANEL, fg=INK2).pack(side='left')
        self.minutes_var = tk.StringVar(value='30')
        ttk.Spinbox(row, from_=0, to=600, increment=5, textvariable=self.minutes_var, width=5
                    ).pack(side='left', padx=(4, 16))

        self.start_btn = ttk.Button(row, text='Start', command=self.start)
        self.start_btn.pack(side='left')
        self.stop_btn = ttk.Button(row, text='Stop', command=self.stop)
        self.stop_btn.pack(side='left', padx=6)

        # Dew point mode: optional sine wave around the hold point
        row2 = tk.Frame(panel, bg=PANEL)
        row2.pack(fill='x', padx=10, pady=(0, 10))
        tk.Label(row2, text='Sine wave around the dew point:  period (s, 0 = hold still):', bg=PANEL, fg=INK2
                 ).pack(side='left')
        self.period_var = tk.StringVar(value='0')
        self.period_spin = ttk.Spinbox(row2, from_=0, to=3600, increment=30, textvariable=self.period_var, width=6)
        self.period_spin.pack(side='left', padx=(4, 16))
        tk.Label(row2, text='amplitude (± °C):', bg=PANEL, fg=INK2).pack(side='left')
        self.amplitude_var = tk.StringVar(value='1')
        self.amplitude_spin = ttk.Spinbox(row2, from_=0, to=5, increment=0.25, textvariable=self.amplitude_var,
                                          width=5)
        self.amplitude_spin.pack(side='left', padx=(4, 16))
        tk.Label(row2, text='The plate needs ~10-20 s to respond, so periods under ~60 s come out smaller and late.',
                 bg=PANEL, fg=MUTED).pack(side='left')

    def build_stats(self):
        row = tk.Frame(self.root, bg=PANEL)
        row.pack(fill='x', padx=12, pady=(0, 6))
        inner = tk.Frame(row, bg=PANEL)
        inner.pack(fill='x', pady=8)
        names = [('elapsed', 'ELAPSED'), ('plate', 'PLATE'), ('target', 'TARGET'), ('dew', 'DEW POINT'),
                 ('air', 'AIR / RH'), ('phase', 'PHASE'), ('voltage', 'PELTIER V'), ('current', 'CURRENT'),
                 ('pwm', 'PWM')]
        self.stats = {}
        for key, label in names:
            self.stats[key] = Stat(inner, label)
            self.stats[key].pack(side='left', expand=True, fill='x')

    def build_body(self):
        body = ttk.PanedWindow(self.root, orient='vertical')
        body.pack(fill='both', expand=True, padx=12, pady=(0, 10))

        self.fig = Figure(figsize=(10, 6), layout='constrained', facecolor=BG)
        self.ax_temp, self.ax_volt, self.ax_curr = self.fig.subplots(3, 1, sharex=True)
        self.l_temp, = self.ax_temp.plot([], [], color=BLUE, label='Plate')
        self.l_target, = self.ax_temp.plot([], [], 'k--', lw=1, label='Target')
        self.l_dew, = self.ax_temp.plot([], [], color=CYAN, ls=':', lw=2, label='Dew point')
        self.l_set, = self.ax_volt.plot([], [], color=ORANGE, label='Set')
        self.l_volt, = self.ax_volt.plot([], [], color=GREEN, label='Measured')
        self.l_curr, = self.ax_curr.plot([], [], color='#8e44ad')
        self.ax_temp.set_ylabel('Temperature (°C)')
        self.ax_volt.set_ylabel('Peltier V\n(+ cool, − heat)')
        self.ax_curr.set_ylabel('Current (A)')
        self.ax_curr.set_xlabel('Time (s)')
        for ax in (self.ax_temp, self.ax_volt, self.ax_curr):
            ax.grid(True, color='#e4e3df')
        self.ax_temp.legend(loc='upper right')
        self.ax_volt.legend(loc='upper right')
        graph = tk.Frame(body, bg=BG)
        self.canvas = FigureCanvasTkAgg(self.fig, master=graph)
        self.canvas.get_tk_widget().pack(fill='both', expand=True)
        body.add(graph, weight=4)

        self.console = ScrolledText(body, height=8, font=('Menlo', 11), bg='#1e1e1e', fg='#d4d4d4',
                                    insertbackground='#d4d4d4', state='disabled')
        body.add(self.console, weight=1)

    # ------------------------------------------------------------------ helpers
    def log(self, text):
        self.console.config(state='normal')
        self.console.insert('end', text.rstrip('\n') + '\n')
        self.console.see('end')
        self.console.config(state='disabled')

    def update_buttons(self):
        idle = self.busy is None
        self.start_btn.config(state='normal' if idle and not self.running else 'disabled')
        self.stop_btn.config(state='normal' if self.running and self.busy != 'stop' else 'disabled')
        self.check_btn.config(state='normal' if idle and not self.running else 'disabled')
        self.fetch_btn.config(state='normal' if idle else 'disabled')
        dew = self.mode.get() == 'dew'
        self.target_spin.config(state='disabled' if dew else 'normal')
        for spin in (self.offset_spin, self.period_spin, self.amplitude_spin):
            spin.config(state='normal' if dew else 'disabled')

    def run_command(self, name, args, on_done=None):
        """Run `run.sh args` in a thread, echoing its output to the console; then on_done(returncode, output)"""
        self.busy = name
        self.update_buttons()
        self.log(f'$ ./run.sh {" ".join(args)}')

        def work():
            proc = subprocess.Popen([RUN_SH, *args], cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding='utf-8', errors='replace')
            output = []
            for line in proc.stdout:
                output.append(line)
                self.events.put(('log', line))
            self.events.put(('done', (name, proc.wait(), ''.join(output), on_done)))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ lab PC state
    def refresh_status(self):
        """Ask the lab PC whether a run is going (silently; repeats every STATUS_POLL_MS)"""
        def work():
            try:
                result = subprocess.run([RUN_SH, 'status'], cwd=HERE, capture_output=True, text=True,
                                        encoding='utf-8', errors='replace', timeout=60)
                self.events.put(('status', (result.returncode, result.stdout + result.stderr)))
            except subprocess.TimeoutExpired:
                self.events.put(('status', (1, "Can't reach the lab PC (timeout)")))

        if self.busy is None:
            threading.Thread(target=work, daemon=True).start()
        self.root.after(STATUS_POLL_MS, self.refresh_status)

    def on_status(self, returncode, output):
        if returncode != 0:
            self.set_status("Can't reach the lab PC over SSH", RED)
            return
        running = output.startswith('Running')
        if running and not self.stream:
            self.log('A run is going on the lab PC; following it.')
            self.set_running(True)
            self.start_stream()
        elif not running and self.running and self.busy is None:
            # Ended without us seeing its last line (e.g. the stream dropped)
            self.set_running(False)
            self.stop_stream()
            self.set_status('Run ended', AMBER)
        elif not running and not self.running:
            self.set_status('Lab PC connected, idle', GREEN)

    def set_running(self, running):
        self.running = running
        if running:
            self.set_status('Experiment running on the lab PC', BLUE)
        self.update_buttons()

    # ------------------------------------------------------------------ actions
    def check(self):
        def done(code, output):
            fails = [l for l in output.splitlines() if l.startswith('FAIL')]
            if code != 0 or fails:
                self.set_status('Hardware check failed: ' + (fails[0][5:] if fails else 'see console'), RED)
            else:
                self.set_status('Hardware OK, idle', GREEN)
        self.run_command('check', ['check'], done)

    def start(self):
        try:
            minutes = float(self.minutes_var.get())
            assert minutes >= 0
            if self.mode.get() == 'dew':
                offset = float(self.offset_var.get())
                period = float(self.period_var.get())
                amplitude = float(self.amplitude_var.get())
                assert period >= 0 and amplitude >= 0
                args = ['start', 'dew', f'{minutes:g}', f'{offset:g}', f'{period:g}', f'{amplitude:g}']
                what = 'the dew point' + (f' + {offset:g} °C' if offset else '')
                if period and amplitude:
                    what += f', then a sine wave of ±{amplitude:g} °C with a {period:g} s period'
            else:
                target = float(self.target_var.get())
                assert TARGET_RANGE[0] <= target <= TARGET_RANGE[1]
                args = ['start', f'{target:g}', f'{minutes:g}']
                what = f'{target:g} °C'
        except (ValueError, AssertionError):
            messagebox.showerror('Invalid settings', f'Check the numbers: target {TARGET_RANGE[0]:g}–'
                                 f'{TARGET_RANGE[1]:g} °C; duration, sine period and amplitude ≥ 0.')
            return
        length = 'until stopped' if minutes == 0 else f'for {minutes:g} min'
        if not messagebox.askokcancel('Start run', f'Power the Peltier and bring the plate to {what}, {length}?'):
            return

        def done(code, output):
            if code == 0 and 'Started' in output:
                self.parser = ConsoleParser()
                self.dirty = True
                self.set_running(True)
                self.root.after(3000, self.start_stream)  # Give main.py a moment to recreate console.log
            else:
                self.set_status('Start failed; see console', RED)
        self.run_command('start', args, done)

    def stop(self):
        def done(code, output):
            if code != 0:
                self.set_status('Stop may have failed; see console', RED)
        self.set_status('Stopping: saving data, switching the supply off…', AMBER)
        self.run_command('stop', ['stop'], done)

    def fetch(self):
        def done(code, output):
            if code == 0:
                self.set_status(output.strip().splitlines()[0].rstrip(':'), GREEN)
        self.run_command('fetch', ['fetch'], done)

    # ------------------------------------------------------------------ live stream
    def start_stream(self):
        if self.stream:
            return
        self.parser = ConsoleParser()
        self.dirty = True
        # Own process group, so stopping it also ends the ssh it starts
        self.stream = subprocess.Popen([RUN_SH, 'stream'], cwd=HERE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                                       start_new_session=True)
        stream = self.stream

        def work():
            for line in stream.stdout:
                self.events.put(('line', line))
            self.events.put(('stream_end', stream))

        threading.Thread(target=work, daemon=True).start()

    def stop_stream(self):
        if self.stream:
            try:
                os.killpg(self.stream.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.stream = None

    def on_line(self, line):
        new_row = self.parser.feed(line)
        if new_row:
            self.dirty = True
        elif not self.parser.is_reading_line(line):
            self.log(line)  # Messages: phase changes, warnings, errors, saved files
        if self.parser.end and self.running:
            self.run_finished()

    def run_finished(self):
        error = self.parser.end.startswith('An error')
        self.set_running(False)
        self.root.after(4000, self.stop_stream)  # Let the "Data saved" lines arrive first
        self.set_status(self.parser.end if error else 'Run finished; fetching results…', RED if error else GREEN)
        self.root.after(5000, self.fetch_when_idle)

    def fetch_when_idle(self):
        """Fetch once no other run.sh command (e.g. a stop still waiting for the run) is in progress"""
        if self.busy:
            self.root.after(1000, self.fetch_when_idle)
        else:
            self.fetch()

    # ------------------------------------------------------------------ event loop
    def poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == 'line':
                    self.on_line(payload)
                elif kind == 'log':
                    self.log(payload)
                elif kind == 'done':
                    name, code, output, on_done = payload
                    self.busy = None
                    self.update_buttons()
                    if on_done:
                        on_done(code, output)
                elif kind == 'status':
                    self.on_status(*payload)
                elif kind == 'stream_end' and payload is self.stream:
                    self.stream = None  # Dropped; the next status poll restarts it if the run is still going
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def redraw_loop(self):
        if self.dirty:
            self.dirty = False
            self.redraw()
        self.root.after(1000, self.redraw_loop)

    def redraw(self):
        rows = self.parser.rows
        t = [r['time'] for r in rows]
        for line, key in ((self.l_temp, 'temperature'), (self.l_target, 'target'), (self.l_dew, 'dew'),
                          (self.l_set, 'set_voltage'), (self.l_volt, 'voltage'), (self.l_curr, 'current')):
            line.set_data(t, [r[key] for r in rows])
        for ax in (self.ax_temp, self.ax_volt, self.ax_curr):
            ax.relim()
            ax.autoscale_view()
        self.canvas.draw_idle()

        if not rows:
            for stat in self.stats.values():
                stat.set('—')
            return
        r = rows[-1]
        known = lambda v: v == v  # not nan
        minutes, seconds = divmod(int(r['time']), 60)
        self.stats['elapsed'].set(f'{minutes}:{seconds:02d}')
        self.stats['plate'].set(f"{r['temperature']:.2f} °C" if known(r['temperature']) else 'error',
                                INK if known(r['temperature']) else RED)
        self.stats['target'].set(f"{r['target']:.2f} °C" if known(r['target']) else '—')
        self.stats['dew'].set(f"{r['dew']:.2f} °C" if known(r['dew']) else '—', CYAN)
        self.stats['air'].set(f"{r['air']:.1f} °C / {r['humidity']:.0f}%" if known(r['air']) else '—')
        self.stats['phase'].set(r['phase'] if isinstance(r['phase'], str) else 'fixed')
        color = BLUE if r['state'] == 'cooling' else ORANGE if r['state'] == 'heating' else INK
        self.stats['voltage'].set(f"{r['voltage']:+.2f} V", color)
        self.stats['current'].set(f"{r['current']:+.3f} A")
        at_limit = r['state'] == 'cooling' and r['pwm'] >= MAX_COOL_PWM
        self.stats['pwm'].set(f"{r['pwm']:.0f}%" + (' max' if at_limit else ''), AMBER if at_limit else INK)

    def close(self):
        if self.running and not messagebox.askokcancel(
                'Close', 'The experiment keeps running on the lab PC (it stops at its time limit, or reopen '
                         'this window and press Stop). Close the window?'):
            return
        self.stop_stream()
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    MacUI(root)
    root.mainloop()
