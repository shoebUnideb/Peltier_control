import glob
import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import ttk, messagebox

import pandas as pd

import matplotlib
matplotlib.use('Agg')  # The controller saves its plots from a worker thread; keep pyplot off Tk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

import serial.tools.list_ports
from main import (control_power_supply_with_temp_monitoring, preflight_check,
                  find_arduino_port, find_power_supply_port,
                  TARGET_TEMPERATURE, TARGET_RANGE, V_MIN, DEW_HOLD_OFFSET)

# -- Palette (light background, one accent per role; text stays off the accent colors) --
BG = '#fcfcfb'
PANEL = '#f3f2ef'
GRID = '#e4e3df'
INK = '#0b0b0b'
INK2 = '#52514e'
MUTED = '#898781'
BLUE = '#2a78d6'      # cooling / primary accent
CYAN = '#1aa3b8'      # dew point
ORANGE = '#eb6834'    # heating
GREEN = '#0ca30c'     # ok
RED = '#c4362f'       # error
AMBER = '#b9790a'     # warning


class LED(tk.Canvas):
    """A small colored status dot."""
    def __init__(self, master, size=10):
        super().__init__(master, width=size, height=size, bg=master['bg'], highlightthickness=0)
        self.size = size
        self.oval = self.create_oval(1, 1, size - 1, size - 1, fill=MUTED, outline='')

    def set(self, ok):
        color = {True: GREEN, False: RED, None: AMBER}[ok]
        self.itemconfig(self.oval, fill=color)


class Stat(tk.Frame):
    """A small labeled readout used in the live dashboard row."""
    def __init__(self, master, label):
        super().__init__(master, bg=PANEL)
        tk.Label(self, text=label, font=('Segoe UI', 8), fg=MUTED, bg=PANEL).pack(anchor='w')
        self.value = tk.Label(self, text='—', font=('Segoe UI', 13, 'bold'), fg=INK, bg=PANEL)
        self.value.pack(anchor='w')

    def set(self, text, color=INK):
        self.value.config(text=text, fg=color)


def find_runs(base_dir='.'):
    """List measurements_<timestamp> folders in `base_dir`, newest first, with their save status."""
    runs = []
    for path in sorted(glob.glob(os.path.join(base_dir, 'measurements_*')), reverse=True):
        if not os.path.isdir(path):
            continue
        name = os.path.basename(path)
        try:
            when = datetime.strptime(name[len('measurements_'):], '%Y%m%d_%H%M%S')
        except ValueError:
            when = None
        ok_xlsx = os.path.join(path, 'power_supply_temp_log.xlsx')
        err_xlsx = os.path.join(path, 'power_supply_temp_log_ERROR.xlsx')
        plot = os.path.join(path, 'measurement_plots.png')
        if os.path.exists(ok_xlsx):
            status, xlsx = 'OK', ok_xlsx
        elif os.path.exists(err_xlsx):
            status, xlsx = 'ERROR', err_xlsx
        else:
            status, xlsx = 'EMPTY', None
        runs.append({'path': os.path.abspath(path), 'when': when, 'status': status, 'xlsx': xlsx,
                    'plot': plot if os.path.exists(plot) else None})
    return runs


def open_with_default_app(path):
    """Open a file or folder with whatever the OS has associated with it."""
    try:
        os.startfile(path)
    except Exception as e:
        messagebox.showerror('Could not open', f'{path}\n\n{e}')


class HistoryWindow(tk.Toplevel):
    """Browse past runs: pick one from the list, preview its plot, open the spreadsheet or its folder."""
    def __init__(self, master, base_dir='.', select=None):
        super().__init__(master, bg=BG)
        self.base_dir = base_dir
        self.title('Past runs')
        self.geometry('900x560')
        self.current = None  # the selected run dict

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill='x', padx=10, pady=8)
        tk.Label(bar, text='Past measurement runs', bg=BG, fg=INK, font=('Segoe UI', 11, 'bold')).pack(side='left')
        tk.Button(bar, text='Refresh', command=lambda: self.refresh(), bg='#e4e3df', relief='flat',
                 padx=10).pack(side='right')

        body = tk.Frame(self, bg=BG)
        body.pack(fill='both', expand=True, padx=10, pady=(0, 10))

        left = tk.Frame(body, bg=BG)
        left.pack(side='left', fill='y')
        self.tree = ttk.Treeview(left, columns=('status',), show='tree headings', selectmode='browse', height=20)
        self.tree.heading('#0', text='Run')
        self.tree.heading('status', text='Status')
        self.tree.column('#0', width=190)
        self.tree.column('status', width=70, anchor='center')
        self.tree.tag_configure('OK', foreground=GREEN)
        self.tree.tag_configure('ERROR', foreground=RED)
        self.tree.tag_configure('EMPTY', foreground=MUTED)
        self.tree.pack(side='left', fill='y')
        self.tree.bind('<<TreeviewSelect>>', lambda e: self.show_selected())

        right = tk.Frame(body, bg=PANEL, highlightbackground=GRID, highlightthickness=1)
        right.pack(side='left', fill='both', expand=True, padx=(10, 0))
        self.detail = tk.Label(right, text='Select a run on the left', bg=PANEL, fg=INK2, justify='left',
                               anchor='nw', font=('Segoe UI', 10), padx=12, pady=10)
        self.detail.pack(fill='x')
        self.preview = tk.Label(right, bg=PANEL, text='', anchor='center')
        self.preview.pack(fill='both', expand=True, padx=12)

        btns = tk.Frame(right, bg=PANEL)
        btns.pack(fill='x', padx=12, pady=10)
        self.btn_plot = tk.Button(btns, text='Open plot image', command=self.open_plot, state='disabled',
                                  bg='#e4e3df', relief='flat', padx=8)
        self.btn_plot.pack(side='left')
        self.btn_xlsx = tk.Button(btns, text='Open spreadsheet', command=self.open_xlsx, state='disabled',
                                  bg='#e4e3df', relief='flat', padx=8)
        self.btn_xlsx.pack(side='left', padx=8)
        self.btn_folder = tk.Button(btns, text='Open folder', command=self.open_folder, state='disabled',
                                    bg='#e4e3df', relief='flat', padx=8)
        self.btn_folder.pack(side='left')

        self.refresh(select)

    def refresh(self, select=None):
        select = select or (self.current and self.current['path'])
        self.tree.delete(*self.tree.get_children())
        self.runs = {r['path']: r for r in find_runs(self.base_dir)}
        for path, r in self.runs.items():
            label = r['when'].strftime('%Y-%m-%d  %H:%M:%S') if r['when'] else os.path.basename(path)
            self.tree.insert('', 'end', iid=path, text=label, values=(r['status'],), tags=(r['status'],))
        if select and select in self.runs:
            self.tree.selection_set(select)
            self.tree.see(select)
            self.show_selected()
        elif self.runs:
            first = next(iter(self.runs))
            self.tree.selection_set(first)
            self.show_selected()
        else:
            self.current = None
            self.detail.config(text='No measurement runs found in this folder yet.\nRun a measurement, then '
                                    'come back here.')
            self.preview.config(image='', text='')
            self.preview.image = None
            for b in (self.btn_plot, self.btn_xlsx, self.btn_folder):
                b.config(state='disabled')

    def show_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        run = self.runs[sel[0]]
        self.current = run
        self.detail.config(text=self.describe(run))
        self.load_preview(run['plot'])
        self.btn_plot.config(state='normal' if run['plot'] else 'disabled')
        self.btn_xlsx.config(state='normal' if run['xlsx'] else 'disabled')
        self.btn_folder.config(state='normal')

    def describe(self, run):
        lines = [f"Folder: {os.path.basename(run['path'])}", f"Status: {run['status']}"]
        if run['xlsx']:
            try:
                df = pd.read_excel(run['xlsx'])
                dew_run = 'Phase' in df and (df['Phase'] != 'fixed').any()
                start, end = df['Timestamp'].iloc[0], df['Timestamp'].iloc[-1]
                duration = (end - start).total_seconds()
                fmt_temp = lambda v: 'no reading' if pd.isna(v) else f'{v:.2f} °C'
                target = (f"dew point (held at {df['Target_Temperature'].iloc[-1]:.2f} °C)" if dew_run
                          else f"{df['Target_Temperature'].iloc[0]:g} °C")
                lines += [f"Target: {target}", f"Duration: {duration:.0f} s ({len(df)} readings)",
                         f"Start → end temperature: {fmt_temp(df['Temperature'].iloc[0])} → "
                         f"{fmt_temp(df['Temperature'].iloc[-1])}"]
            except Exception as e:
                lines.append(f"(Could not read the spreadsheet: {e})")
        else:
            lines.append('No data was saved for this run.')
        return '\n'.join(lines)

    def load_preview(self, plot_path):
        if not plot_path:
            self.preview.config(image='', text='No plot available for this run')
            self.preview.image = None
            return
        try:
            img = tk.PhotoImage(file=plot_path)
            factor = max(1, img.width() // 720)
            if factor > 1:
                img = img.subsample(factor, factor)
            self.preview.config(image=img, text='')
            self.preview.image = img  # keep a reference so Tk doesn't garbage-collect it
        except Exception as e:
            self.preview.config(image='', text=f'Could not load the plot: {e}')
            self.preview.image = None

    def open_plot(self):
        self.current and self.current['plot'] and open_with_default_app(self.current['plot'])

    def open_xlsx(self):
        self.current and self.current['xlsx'] and open_with_default_app(self.current['xlsx'])

    def open_folder(self):
        self.current and open_with_default_app(self.current['path'])


class ControllerUI:
    def __init__(self, root):
        self.root = root
        self.readings = queue.Queue()
        self.check_queue = queue.Queue()
        self.stop_event = None
        self.worker = None
        self.checking = False
        self.error = None
        self.last_run_folder = None
        self.history_win = None
        self.t, self.temp, self.volt, self.curr, self.targets, self.dews = [], [], [], [], [], []
        self.t0 = None
        self.target = TARGET_TEMPERATURE

        root.title('Peltier PID Controller')
        root.configure(bg=BG)
        root.geometry('1180x830')

        self.build_connection_panel()
        self.build_banner()
        self.build_control_panel()
        self.build_stats_row()
        self.build_graphs()

        root.protocol('WM_DELETE_WINDOW', self.close)
        self.refresh_ports()
        self.check_connections()
        self.poll()

    # ---------------------------------------------------------------- connection panel
    def build_connection_panel(self):
        panel = tk.Frame(self.root, bg=PANEL, highlightbackground=GRID, highlightthickness=1)
        panel.pack(fill='x', padx=10, pady=(10, 0))

        row = tk.Frame(panel, bg=PANEL)
        row.pack(fill='x', padx=10, pady=(8, 4))
        tk.Label(row, text='Arduino port:', bg=PANEL, fg=INK2).pack(side='left')
        self.arduino_var = tk.StringVar()
        self.arduino_box = ttk.Combobox(row, textvariable=self.arduino_var, width=28, state='readonly')
        self.arduino_box.pack(side='left', padx=(4, 16))
        tk.Label(row, text='Power supply port:', bg=PANEL, fg=INK2).pack(side='left')
        self.ps_var = tk.StringVar()
        self.ps_box = ttk.Combobox(row, textvariable=self.ps_var, width=28, state='readonly')
        self.ps_box.pack(side='left', padx=(4, 16))
        self.check_btn = tk.Button(row, text='Check connections', command=self.check_connections,
                                   bg='#e4e3df', activebackground='#d8d7d2', relief='flat', padx=10)
        self.check_btn.pack(side='left')

        self.checks_frame = tk.Frame(panel, bg=PANEL)
        self.checks_frame.pack(fill='x', padx=10, pady=(0, 8))
        self.check_rows = []  # list of (LED, name_label, message_label)

    def refresh_ports(self):
        ports = list(serial.tools.list_ports.comports())
        options = ['Auto-detect'] + [f'{p.device} — {p.description}' for p in ports]

        def repopulate(box, var, auto_finder):
            current = var.get()
            box['values'] = options
            if current in options:
                var.set(current)
            else:
                auto = auto_finder()
                match = next((o for o in options if o.startswith(f'{auto} —')), None) if auto else None
                var.set(match or 'Auto-detect')

        repopulate(self.arduino_box, self.arduino_var, find_arduino_port)
        repopulate(self.ps_box, self.ps_var, find_power_supply_port)

    @staticmethod
    def _port_of(selection):
        return None if not selection or selection == 'Auto-detect' else selection.split(' — ')[0]

    def selected_arduino_port(self):
        return self._port_of(self.arduino_var.get())

    def selected_ps_port(self):
        return self._port_of(self.ps_var.get())

    def check_connections(self):
        if self.worker or self.checking:
            return
        self.checking = True
        self.check_btn.config(state='disabled', text='Checking…')
        self.refresh_ports()
        arduino_port, ps_port = self.selected_arduino_port(), self.selected_ps_port()

        def run():
            try:
                checks = preflight_check(arduino_port, ps_port)
            except Exception as e:
                checks = [{'name': 'Connection check', 'ok': False, 'message': str(e)}]
            self.check_queue.put(checks)

        threading.Thread(target=run, daemon=True).start()

    def show_checks(self, checks):
        for widgets in self.check_rows:
            for w in widgets:
                w.destroy()
        self.check_rows = []
        for c in checks:
            row = tk.Frame(self.checks_frame, bg=PANEL)
            row.pack(fill='x', pady=1)
            led = LED(row)
            led.pack(side='left', padx=(2, 6))
            led.set(c['ok'])
            name = tk.Label(row, text=c['name'] + ':', bg=PANEL, fg=INK, font=('Segoe UI', 9, 'bold'), width=18, anchor='w')
            name.pack(side='left')
            color = {True: INK2, False: RED, None: AMBER}[c['ok']]
            msg = tk.Label(row, text=c['message'], bg=PANEL, fg=color, font=('Segoe UI', 9), anchor='w')
            msg.pack(side='left')
            self.check_rows.append((row, led, name, msg))

        bad = [c for c in checks if c['ok'] is False]
        warn = [c for c in checks if c['ok'] is None]
        if bad:
            names = ', '.join(c['name'] for c in bad)
            self.set_banner(f"⚠ Fix before starting: {names}", RED)
        elif warn:
            names = ', '.join(c['name'] for c in warn)
            self.set_banner(f"Note: {names} — run will proceed without it", AMBER)
        else:
            self.set_banner('✓ Arduino and power supply both responding', GREEN)

    # ---------------------------------------------------------------- warning banner
    def build_banner(self):
        self.banner = tk.Label(self.root, text='Checking connections…', bg=BG, fg=MUTED,
                               font=('Segoe UI', 10, 'bold'), anchor='w')
        self.banner.pack(fill='x', padx=14, pady=(6, 0))

    def set_banner(self, text, color):
        self.banner.config(text=text, fg=color)

    # ---------------------------------------------------------------- controls
    def build_control_panel(self):
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill='x', padx=10, pady=8)

        self.mode_var = tk.StringVar(value='fixed')
        self.mode_buttons = []
        for text, value in (('Fixed target', 'fixed'), ('Dew point', 'dew')):
            b = tk.Radiobutton(bar, text=text, value=value, variable=self.mode_var, command=self.update_mode,
                               bg=BG, fg=INK, activebackground=BG, selectcolor=BG)
            b.pack(side='left')
            self.mode_buttons.append(b)

        tk.Label(bar, text=f'Target (°C, {TARGET_RANGE[0]:g}–{TARGET_RANGE[1]:g}):', bg=BG, fg=INK2).pack(side='left',
                                                                                                padx=(12, 0))
        self.target_var = tk.StringVar(value=str(TARGET_TEMPERATURE))
        self.target_spin = tk.Spinbox(bar, from_=TARGET_RANGE[0], to=TARGET_RANGE[1], increment=0.5,
                                      textvariable=self.target_var, width=7, justify='center')
        self.target_spin.pack(side='left', padx=(6, 12))

        tk.Label(bar, text='Hold above dew point (°C):', bg=BG, fg=INK2).pack(side='left')
        self.offset_var = tk.StringVar(value=f'{DEW_HOLD_OFFSET:g}')
        self.offset_spin = tk.Spinbox(bar, from_=-2, to=10, increment=0.5, textvariable=self.offset_var, width=5,
                                      justify='center')
        self.offset_spin.pack(side='left', padx=(6, 16))

        self.start_btn = tk.Button(bar, text='▶  Start', width=12, command=self.start,
                                   bg=GREEN, fg='white', activebackground='#0a8f0a', relief='flat',
                                   font=('Segoe UI', 10, 'bold'))
        self.start_btn.pack(side='left')
        self.stop_btn = tk.Button(bar, text='■  Stop', width=12, command=self.stop, state='disabled',
                                  bg=RED, fg='white', activebackground='#a92c26', relief='flat',
                                  font=('Segoe UI', 10, 'bold'))
        self.stop_btn.pack(side='left', padx=8)
        tk.Button(bar, text='📁 Past runs', command=self.open_history, bg='#e4e3df', activebackground='#d8d7d2',
                 relief='flat', padx=10).pack(side='left', padx=(8, 0))

        self.status = tk.Label(bar, text='Idle', bg=BG, fg=INK2)
        self.status.pack(side='left', padx=16)
        self.update_mode()

    def update_mode(self, running=False):
        """Enable the inputs that apply to the selected mode (none while a run is going)"""
        dew = self.mode_var.get() == 'dew'
        self.target_spin.config(state='disabled' if dew or running else 'normal')
        self.offset_spin.config(state='normal' if dew and not running else 'disabled')
        for b in self.mode_buttons:
            b.config(state='disabled' if running else 'normal')

    def open_history(self, select=None):
        select = select or self.last_run_folder
        if self.history_win and self.history_win.winfo_exists():
            self.history_win.refresh(select)
            self.history_win.lift()
            self.history_win.focus_force()
        else:
            self.history_win = HistoryWindow(self.root, select=select)

    # ---------------------------------------------------------------- live stats
    def build_stats_row(self):
        row = tk.Frame(self.root, bg=PANEL, highlightbackground=GRID, highlightthickness=1)
        row.pack(fill='x', padx=10, pady=(0, 8))
        inner = tk.Frame(row, bg=PANEL)
        inner.pack(padx=10, pady=8)
        self.stat_elapsed = Stat(inner, 'ELAPSED')
        self.stat_temp = Stat(inner, 'TEMPERATURE')
        self.stat_mode = Stat(inner, 'MODE')
        self.stat_volt = Stat(inner, 'SET VOLTAGE')
        self.stat_curr = Stat(inner, 'CURRENT')
        self.stat_target = Stat(inner, 'TARGET')
        self.stat_dew = Stat(inner, 'DEW POINT')
        self.stat_air = Stat(inner, 'AIR / RH')
        self.stat_phase = Stat(inner, 'PHASE')
        for s in (self.stat_elapsed, self.stat_temp, self.stat_target, self.stat_dew, self.stat_air, self.stat_phase,
                  self.stat_mode, self.stat_volt, self.stat_curr):
            s.pack(side='left', padx=14)

    # ---------------------------------------------------------------- graphs
    def build_graphs(self):
        fig = Figure(figsize=(14, 5.6), layout='constrained', facecolor=BG)
        gs = fig.add_gridspec(2, 2, height_ratios=[1.4, 1])
        ax_t = fig.add_subplot(gs[0, :])
        ax_v = fig.add_subplot(gs[1, 0])
        ax_i = fig.add_subplot(gs[1, 1])
        self.axes = (ax_t, ax_v, ax_i)

        for ax in self.axes:
            ax.set_facecolor(BG)
            ax.grid(True, color=GRID, linewidth=0.8)
            ax.tick_params(colors=MUTED, labelsize=8)
            for side in ('top', 'right'):
                ax.spines[side].set_visible(False)
            for side in ('left', 'bottom'):
                ax.spines[side].set_color(GRID)

        ax_t.set_title('Temperature', loc='left', color=INK, fontsize=10, fontweight='bold')
        ax_t.set_ylabel('°C', color=INK2, fontsize=8)
        ax_v.set_title('Set voltage  (+ cool / − heat)', loc='left', color=INK, fontsize=10, fontweight='bold')
        ax_v.set_ylabel('V', color=INK2, fontsize=8)
        ax_v.set_xlabel('Time (s)', color=INK2, fontsize=8)
        ax_i.set_title('Current', loc='left', color=INK, fontsize=10, fontweight='bold')
        ax_i.set_ylabel('A', color=INK2, fontsize=8)
        ax_i.set_xlabel('Time (s)', color=INK2, fontsize=8)

        self.band = ax_t.axhspan(self.target - 0.1, self.target + 0.1, color=GRID, alpha=0.7, lw=0)
        self.target_line, = ax_t.plot([], [], color=INK2, linestyle='--', linewidth=1, label='Target')
        self.dew_line, = ax_t.plot([], [], color=CYAN, linestyle=':', linewidth=2, label='Dew point')
        self.temp_line, = ax_t.plot([], [], color=BLUE, linewidth=2, label='Plate')
        ax_t.legend(loc='upper right', fontsize=8, frameon=False)
        ax_t.set_xlim(0, 60)
        ax_t.set_ylim(self.target - 3, self.target + 3)
        ax_v.axhline(0, color=MUTED, linewidth=0.8)
        self.volt_line, = ax_v.plot([], [], color=BLUE, linewidth=1.6)
        ax_v.set_xlim(0, 60)
        ax_v.set_ylim(-3.5, 6.5)
        ax_i.axhline(0, color=MUTED, linewidth=0.8)
        self.curr_line, = ax_i.plot([], [], color=BLUE, linewidth=1.6)
        ax_i.set_xlim(0, 60)
        ax_i.set_ylim(-1.5, 2.5)

        self.canvas = FigureCanvasTkAgg(fig, master=self.root)
        self.canvas.get_tk_widget().pack(fill='both', expand=True, padx=10, pady=(0, 10))

    # ---------------------------------------------------------------- start/stop
    def start(self):
        dew = self.mode_var.get() == 'dew'
        try:
            target = float(self.target_var.get())
            offset = float(self.offset_var.get())
        except ValueError:
            target = offset = None
        if offset is None or (not dew and not TARGET_RANGE[0] <= target <= TARGET_RANGE[1]):
            self.status.config(text=f'Enter a target between {TARGET_RANGE[0]:g} and {TARGET_RANGE[1]:g} °C'
                                    ' and a number for the dew point offset')
            return

        bad = [row for row in self.check_rows if row[1].itemcget(row[1].oval, 'fill') == RED]
        if bad:
            proceed = messagebox.askyesno(
                'Connection problem',
                'The last connection check found a problem:\n\n' + self.banner.cget('text') +
                '\n\nStart anyway?')
            if not proceed:
                return

        self.target = target
        self.band.set_bounds(0, target - 0.1, 1, 0.2)  # axhspan returns a Rectangle: (x0, y0, width, height)
        self.band.set_visible(not dew)  # The dew point target moves, so no fixed band
        self.canvas.draw_idle()

        self.t, self.temp, self.volt, self.curr, self.targets, self.dews = [], [], [], [], [], []
        self.t0 = None
        self.error = None
        self.stop_event = threading.Event()
        arduino_port, ps_port = self.selected_arduino_port(), self.selected_ps_port()

        def run():
            self.error, folder = control_power_supply_with_temp_monitoring(
                target, self.stop_event, self.readings.put, arduino_port, ps_port, dew=dew, dew_hold_offset=offset)
            self.last_run_folder = os.path.abspath(folder) if folder else None

        self.worker = threading.Thread(target=run)
        self.worker.start()
        self.update_mode(running=True)
        self.arduino_box.config(state='disabled')
        self.ps_box.config(state='disabled')
        self.check_btn.config(state='disabled')
        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        for stat in (self.stat_target, self.stat_dew, self.stat_air, self.stat_phase):
            stat.set('—')
        what = 'the dew point' + (f' + {offset:g} °C' if offset else '') if dew else f'target {target:g} °C'
        self.status.config(text=f'Starting, {what}…')

    def stop(self):
        self.stop_event.set()
        self.stop_btn.config(state='disabled')
        self.status.config(text='Stopping: power supply off, saving data…')

    # ---------------------------------------------------------------- polling
    def poll(self):
        new_data = False
        while not self.check_queue.empty():
            self.show_checks(self.check_queue.get())
            self.checking = False
            self.check_btn.config(state='normal', text='Check connections')

        while not self.readings.empty():
            row = self.readings.get()
            self.t0 = self.t0 or row['Timestamp']
            self.t.append((row['Timestamp'] - self.t0).total_seconds())
            self.temp.append(float('nan') if row['Temperature'] is None else row['Temperature'])
            self.volt.append(row['Set_Voltage'])
            self.curr.append(row['Measured_Current'])
            self.targets.append(row['Target_Temperature'])
            self.dews.append(float('nan') if row['Dew_Point'] is None else row['Dew_Point'])
            self.stat_target.set(f"{row['Target_Temperature']:.2f} °C")
            self.stat_dew.set('—' if row['Dew_Point'] is None else f"{row['Dew_Point']:.2f} °C", CYAN)
            if row['Air_Temperature'] is not None and row['Humidity'] is not None:
                self.stat_air.set(f"{row['Air_Temperature']:.1f} °C / {row['Humidity']:.0f}%")
            self.stat_phase.set(row['Phase'])
            new_data = True

        if new_data:
            self.temp_line.set_data(self.t, self.temp)
            self.target_line.set_data(self.t, self.targets)
            self.dew_line.set_data(self.t, self.dews)
            self.volt_line.set_data(self.t, self.volt)
            self.curr_line.set_data(self.t, self.curr)
            for ax in self.axes:
                ax.relim()
                ax.autoscale_view()
            self.canvas.draw_idle()

            v, i, t = self.volt[-1], self.curr[-1], self.temp[-1]
            mode, color = ('Off', MUTED)
            if abs(v) >= V_MIN:
                mode, color = ('Cooling', BLUE) if v > 0 else ('Heating', ORANGE)
            self.stat_elapsed.set(f'{self.t[-1]:.0f} s')
            self.stat_temp.set(f'{t:.2f} °C' if t == t else 'error', RED if t != t else INK)
            self.stat_mode.set(mode, color)
            self.stat_volt.set(f'{v:+.2f} V')
            self.stat_curr.set(f'{i:+.2f} A')
            self.status.config(text=f'Running  |  t = {self.t[-1]:.0f} s   T = {t:.2f} °C   V = {v:+.2f} V ({mode.lower()})')

        if self.worker and not self.worker.is_alive():
            self.worker = None
            self.update_mode()
            self.arduino_box.config(state='readonly')
            self.ps_box.config(state='readonly')
            self.check_btn.config(state='normal')
            self.start_btn.config(state='normal')
            self.stop_btn.config(state='disabled')
            if self.error:
                self.status.config(text=f'Stopped with error: {self.error}  (see Past runs)')
                self.stat_mode.set('Error', RED)
            else:
                self.status.config(text='Stopped: power supply off, data saved  (see Past runs)')
                self.stat_mode.set('Idle', MUTED)
            if self.history_win and self.history_win.winfo_exists():
                self.history_win.refresh(self.last_run_folder)

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
