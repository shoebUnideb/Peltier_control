"""Parse main.py's console output (one block of lines per reading) back into data rows.

Used on this machine by live_plot.py and mac_ui.py, which get the lab PC's console.log over SSH.
"""
import re

PATTERNS = {
    'elapsed': re.compile(r'Elapsed time: ([\d.]+)s'),
    'set_voltage': re.compile(r'Set Voltage: ([+-]?[\d.]+)V \((\w+), PWM (\d+)%'),
    'voltage': re.compile(r'Measured Voltage: ([+-]?[\d.]+)V'),
    'current': re.compile(r'Measured Current: ([+-]?[\d.]+)A'),
    'air': re.compile(r'Air: ([+-]?[\d.]+)°C, RH ([\d.]+)%, dew point ([+-]?[\d.]+)°C(?:, phase (\w+))?'),
    'temperature': re.compile(r'Temperature: (?:([+-]?[\d.]+)°C \(target ([+-]?[\d.]+)°C\)|Reading Error)'),
}
# Lines that mean the run is over
END_MARKERS = ('Measurement stopped', 'Program stopped by user', 'An error occurred')
# Row keys; a value the output didn't contain is nan
KEYS = ('time', 'temperature', 'target', 'set_voltage', 'state', 'pwm', 'voltage', 'current',
        'air', 'humidity', 'dew', 'phase')
NAN = float('nan')


class ConsoleParser:
    """Feed lines with `feed`; completed readings collect in `rows`, the end-of-run line in `end`."""
    def __init__(self):
        self.rows = []
        self.end = None
        self._row = {}

    def feed(self, line):
        """Parse one line. Returns True if it completed a reading (appended to `rows`)."""
        line = line.strip()
        if line.startswith(END_MARKERS):
            self.end = line
        for key, pattern in PATTERNS.items():
            match = pattern.match(line)
            if not match:
                continue
            if key == 'elapsed':
                self._row = {'time': float(match.group(1))}
            elif key == 'set_voltage':
                self._row.update(set_voltage=float(match.group(1)), state=match.group(2), pwm=float(match.group(3)))
            elif key == 'air':
                self._row.update(air=float(match.group(1)), humidity=float(match.group(2)),
                                 dew=float(match.group(3)), phase=match.group(4))
            elif key == 'temperature':
                if match.group(1):
                    self._row.update(temperature=float(match.group(1)), target=float(match.group(2)))
                return self._finish_row()
            else:
                self._row[key] = float(match.group(1))
        return False

    def _finish_row(self):
        """The Temperature line ends a reading's block"""
        row, self._row = self._row, {}
        if 'time' not in row:  # Joined the stream mid-block
            return False
        if self.rows and row['time'] < self.rows[-1]['time']:  # A new run overwrote console.log
            self.rows.clear()
            self.end = None
        self.rows.append({key: row.get(key, NAN) for key in KEYS})
        return True

    def is_reading_line(self, line):
        """Whether `line` is part of a reading block (as opposed to a message such as a phase change)"""
        line = line.strip()
        return (not line or set(line) == {'-'} or line.startswith('Time:')
                or any(pattern.match(line) for pattern in PATTERNS.values()))
