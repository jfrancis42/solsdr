#!/usr/bin/env python3
"""
solsdr panadapter + control panel for the SunSDR2 PRO.

A standalone, visually-nice panadapter that reads solsdr's raw-IQ TCP stream
(`python3 -m solsdr`, IQ server on port 5555, on by default) and its text
control API (port 5556) for live radio state AND control. Pure Python: PyQt5 +
pyqtgraph + numpy. No GNU Radio, no ExpertSDR3.

CONTROL PANEL: it both displays and controls the radio — click-to-tune, a mode
selector, and clickable frequency digits. Every control mirrors the radio's live
state (polled from the control API), so changes made elsewhere (the shell, other
clients) are reflected here, and vice-versa. Controls need solsdr's control API
(on by default). Without it, the panadapter still works as a display.

Features
--------
  * Radio control (needs the control API), grouped into toolbar rows by function:
      Row 1 (Radio):  mode dropdown, clickable frequency dial (click a digit's
        top to step that decade up, bottom down; "0" zeroes below the kHz
        decimal), filter bandwidth presets (CW 200/400/600 Hz, SSB 2.4/2.7/3.0
        kHz + Custom), filter skirt sharpness (soft/normal/sharp), preamp.
      Row 2 (RX DSP): AGC, manual gain, RIT, NR, NB, notch, APF, squelch.
      Row 3 (Display): the view-only controls (below) — never touches the radio.
    Every radio control mirrors the live state, so shell/other-client changes
    move the widgets here and vice-versa. Also click the spectrum/waterfall to
    tune.
  * Filter passband overlay: a translucent region shows the RX filter (drag its
    edges on the spectrum to set the passband; RF offsets from the dial), and a
    solid line marks the exact dial frequency.
  * Live FFT spectrum (top) + scrolling waterfall (bottom), sharing one
    absolute-frequency x-axis so a peak sits directly over its waterfall trace.
  * Frequency across the bottom (MHz), amplitude up the side (dBFS, or dBm with
    a calibration offset).
  * Auto-scale (tracks noise floor + peaks) or fixed scale (ref level + range).
  * Automatically adapts to solsdr's sample rate + center frequency from the
    stream header, and follows retunes via the control API.
  * Strong signal-vs-noise contrast: perceptual colormaps (inferno/viridis/…),
    spectrum fill, and waterfall intensity linked to the spectrum scale.
  * Live readout of the frequency + level under the mouse (crosshair).
  * Info bar: tuned freq, mode, PTT, TX power setpoint, S-meter, sample rate,
    span, and resolution bandwidth.
  * Resizable window; drag the splitter to trade spectrum height for waterfall.
  * Frequency zoom (+/- toolbar buttons or +/- keys, 0 = full span), centered on
    the tuned frequency; spectrum and waterfall stay aligned.
  * Averaging, peak-hold, DC-spike suppression, adjustable FFT size, freeze.
  * Fast by default (thin non-antialiased trace + periodic rescale) so it hits
    30 fps+ even on a CPU-only box; --pretty for a filled antialiased trace on a
    GPU/fast machine.

Usage
-----
    # live: on the machine running solsdr (RX IQ + control API on by default) —
    python3 -m solsdr 14074

    # then, anywhere that can reach it (needs a display; ssh -X for remote):
    python3 clients/panadapter.py --host 127.0.0.1

    # demo / offline: replay a recorded capture, no radio needed —
    python3 clients/panadapter.py --file clients/examples/solsdr_20m_demo30.iq

Options: --host, --port (IQ, 5555), --control-port (5556), --file (replay a
wire-format capture; loops at EOF), --fft, --ref-offset (dBFS→dBm), --rescale
(auto-scale cadence, s), --pretty (antialiased filled trace), --no-control. Run
with --help for all. Keys: +/- zoom (0 = full) · A auto-scale · R rescale now ·
P peak-hold · C colormap · space freeze · Q quit. (Zoom +/- buttons are in the
toolbar; mouse-drag/scroll on the plot also zooms/pans.) Left-click the
spectrum/waterfall to tune the radio.

Notes
-----
  * solsdr RX has no absolute power calibration, so the axis is **dBFS** by
    default (0 dBFS = a full-scale complex tone). If you have measured the
    offset for your setup, pass --ref-offset <dB> to relabel the axis as dBm.
  * solsdr's control API reports freq/mode/PTT/power/S-meter AND the RX DSP /
    front-end state (AGC, gain, RIT, NR, NB, notch, APF, squelch, preamp), so the
    second-row controls mirror the live radio — changes made in the shell or
    another client move the widgets here, and vice-versa.
"""
import argparse
import os
import socket
import sys
import threading
import time

import numpy as np

try:
    import pyqtgraph as pg
    from pyqtgraph.Qt import QtCore, QtGui, QtWidgets
except ImportError:
    sys.stderr.write("this panadapter needs pyqtgraph + a Qt binding:\n"
                     "    pip install pyqtgraph PyQt5   # (or PyQt6 / PySide6)\n")
    raise


def _qt(enum_class, member):
    """Resolve a Qt enum member across bindings: PyQt6/PySide6 use scoped enums
    (Qt.Orientation.Horizontal), PyQt5 uses flat (Qt.Horizontal). pyqtgraph may
    bind to any of them, so resolve both ways."""
    scope = getattr(QtCore.Qt, enum_class, None)
    if scope is not None and hasattr(scope, member):
        return getattr(scope, member)
    return getattr(QtCore.Qt, member)


# Enum constants used throughout (binding-agnostic).
HORIZONTAL = _qt("Orientation", "Horizontal")
VERTICAL = _qt("Orientation", "Vertical")
RICHTEXT = _qt("TextFormat", "RichText")
DASHLINE = _qt("PenStyle", "DashLine")
DOTLINE = _qt("PenStyle", "DotLine")
KEY_A = _qt("Key", "Key_A")
KEY_P = _qt("Key", "Key_P")
KEY_R = _qt("Key", "Key_R")
KEY_SPACE = _qt("Key", "Key_Space")
KEY_C = _qt("Key", "Key_C")
KEY_Q = _qt("Key", "Key_Q")
KEY_ESCAPE = _qt("Key", "Key_Escape")

# Modes the solsdr control API accepts (see api/control_api.py VALID_MODES).
CONTROL_MODES = ["USB", "LSB", "AM", "FM", "CW"]
# SSB filter skirt sharpness (matches Demodulator.SHARPNESS_PROFILES).
SHARPNESS_CHOICES = ["soft", "normal", "sharp"]


class TenthsAxisItem(pg.AxisItem):
    """Frequency axis that adds MINOR tick marks at tenths of the labeled
    (major) spacing — nine unlabeled subticks between each printed frequency.

    We force exactly two tick levels: the major level pyqtgraph would normally
    label, and a minor level at 1/10 of that spacing. The minor level's strings
    are blanked so those ticks are bare marks (no clutter of numbers)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._minor_spacing = None
        # Longer physical tick stubs below the axis (default -5) so both major and
        # minor marks are easy to see; per-level BRIGHTNESS is boosted in
        # generateDrawSpecs (pyqtgraph otherwise dims minor ticks to ~half alpha,
        # making them nearly invisible over the FFT trace).
        self.setStyle(tickLength=-12)

    def tickValues(self, minVal, maxVal, size):
        base = super().tickValues(minVal, maxVal, size)
        if not base or not base[0][0] or base[0][0] <= 0:
            self._minor_spacing = None
            return base
        major_spacing = base[0][0]
        minor_spacing = major_spacing / 10.0
        self._minor_spacing = minor_spacing
        lo, hi = min(minVal, maxVal), max(minVal, maxVal)
        import math
        v = math.ceil(lo / minor_spacing) * minor_spacing
        minor = []
        while v <= hi and len(minor) < 2000:      # cap: runaway zoom-out guard
            minor.append(v)
            v += minor_spacing
        # major (labeled) level first, then the finer minor level.
        return [base[0], (minor_spacing, minor)]

    def tickStrings(self, values, scale, spacing):
        # blank the minor level (its spacing == major/10) so it draws bare ticks.
        if (self._minor_spacing is not None
                and abs(spacing - self._minor_spacing) < self._minor_spacing * 1e-6):
            return ['' for _ in values]
        return super().tickStrings(values, scale, spacing)

    def generateDrawSpecs(self, p):
        """Brighten the tick/grid pens so the minor (tenths) marks read clearly.

        pyqtgraph dims deeper tick levels: with the grid on, minor grid lines get
        roughly half the alpha of the major ones and nearly vanish under the FFT
        trace. Each tickSpec is (pen, p1, p2). We DON'T change geometry (lengths
        are the grid-line heights — flattening them would erase the grid);
        instead we raise each pen's alpha to a clear floor and keep major lines
        brightest. Longest lines = major (labeled), shorter = minor."""
        specs = super().generateDrawSpecs(p)
        if specs is None:
            return specs
        axisSpec, tickSpecs, textSpecs = specs
        if not tickSpecs:
            return specs
        # pyqtgraph tags the level by pen ALPHA: major grid lines get a higher
        # alpha than minor ones (measured ~63 vs ~18 at our grid setting). Key off
        # that to boost each to a clearly visible floor while keeping major >
        # minor so the tenths read as subticks, not equals.
        alphas = [pen.color().alpha() for pen, _, _ in tickSpecs]
        amax = max(alphas) if alphas else 0
        new = []
        for (pen, p1, p2), a in zip(tickSpecs, alphas):
            pen = QtGui.QPen(pen)
            col = QtGui.QColor(pen.color())
            is_major = amax > 0 and a >= 0.5 * amax
            col.setAlpha(210 if is_major else 120)   # was ~63 / ~18
            pen.setColor(col)
            new.append((pen, p1, p2))
        return (axisSpec, new, textSpecs)


def _qt_frameshape(member):
    """QFrame.Shape.VLine (scoped) or QFrame.VLine (flat), binding-agnostic."""
    scope = getattr(QtWidgets.QFrame, "Shape", None)
    if scope is not None and hasattr(scope, member):
        return getattr(scope, member)
    return getattr(QtWidgets.QFrame, member)


# ─────────────────────────────────────────────────────────────────────────────
# IQ ring-buffer base — shared by the socket and file readers
# ─────────────────────────────────────────────────────────────────────────────
class _RingReader(threading.Thread):
    """Threaded reader holding a rolling buffer of the most recent complex64
    samples. Subclasses fill it via _write(); consumers call latest(n).

    The wire format both readers understand is a text header line then a
    continuous little-endian interleaved float32 I,Q (numpy complex64) stream:
        SOLSDR IQ rate=39062.5 fmt=complex64 freq=14074000\\n<samples...>
    """

    def __init__(self, ring_samples=1 << 20):
        super().__init__(daemon=True)
        self._ring = np.zeros(ring_samples, dtype=np.complex64)
        self._ring_n = ring_samples
        self._wpos = 0
        self._filled = 0
        self._lock = threading.Lock()
        self._running = True
        self.rate = None
        self.header_freq = None
        self.connected = False
        self.status_text = ""

    @staticmethod
    def _parse_header_line(line):
        fields = {}
        for tok in line.split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                fields[k] = v
        rate = float(fields.get("rate", 39062.5))
        freq = float(fields.get("freq", 0.0))
        return rate, freq

    def _write(self, samples):
        m = len(samples)
        with self._lock:
            if m >= self._ring_n:
                self._ring[:] = samples[-self._ring_n:]
                self._wpos = 0
                self._filled = self._ring_n
                return
            end = self._wpos + m
            if end <= self._ring_n:
                self._ring[self._wpos:end] = samples
            else:
                first = self._ring_n - self._wpos
                self._ring[self._wpos:] = samples[:first]
                self._ring[:m - first] = samples[first:]
            self._wpos = end % self._ring_n
            self._filled = min(self._ring_n, self._filled + m)

    def latest(self, n):
        """Return the most recent n complex64 samples (or None if not enough)."""
        with self._lock:
            if self._filled < n:
                return None
            start = (self._wpos - n) % self._ring_n
            if start + n <= self._ring_n:
                return self._ring[start:start + n].copy()
            first = self._ring_n - start
            out = np.empty(n, dtype=np.complex64)
            out[:first] = self._ring[start:]
            out[first:] = self._ring[:n - first]
            return out

    def stop(self):
        self._running = False


# ─────────────────────────────────────────────────────────────────────────────
# IQ stream reader — live, from solsdr's TCP IQ server (background thread)
# ─────────────────────────────────────────────────────────────────────────────
class IQReader(_RingReader):
    """Connects to solsdr's IQ server, parses the one-line header, and keeps a
    rolling buffer of the most recent complex64 samples. Auto-reconnects."""

    def __init__(self, host, port, ring_samples=1 << 20):
        super().__init__(ring_samples)
        self.host = host
        self.port = port
        self.status_text = "connecting…"

    # -- header --------------------------------------------------------------
    @staticmethod
    def _read_header(sock):
        buf = bytearray()
        while b"\n" not in buf and len(buf) < 512:
            b = sock.recv(1)
            if not b:
                break
            buf += b
        line = buf.split(b"\n", 1)[0].decode("ascii", "replace").strip()
        fields = {}
        for tok in line.split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                fields[k] = v
        rate = float(fields.get("rate", 39062.5))
        freq = float(fields.get("freq", 0.0))
        return rate, freq, line

    # -- lifecycle -----------------------------------------------------------
    def run(self):
        while self._running:
            try:
                sock = socket.create_connection((self.host, self.port),
                                                timeout=5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.rate, self.header_freq, line = self._read_header(sock)
                self.connected = True
                self.status_text = f"connected · {line}"
                self._pump(sock)
            except OSError as e:
                self.connected = False
                self.status_text = (f"no IQ server at {self.host}:{self.port} "
                                    f"({e}); retrying…")
                time.sleep(1.5)
            finally:
                try:
                    sock.close()
                except Exception:
                    pass
                self.connected = False

    def _pump(self, sock):
        # Drain-to-live reader. The IQ server pushes ~39k samples/s continuously;
        # if this client ever falls behind (a GC pause, a slow repaint), the
        # unread bytes pile up in the TCP buffers and — because we only need the
        # NEWEST samples for the display — that backlog would otherwise grow
        # without bound, so the waterfall/FFT would drift seconds behind live and
        # get worse the longer the panadapter runs. After each blocking recv we
        # greedily pull everything else already waiting (non-blocking) in one
        # batch, so we always process the freshest IQ and never accumulate lag.
        leftover = b""
        while self._running:
            try:
                data = sock.recv(1 << 20)
            except OSError:
                break
            if not data:
                break
            # non-blocking drain of any further buffered data this instant
            sock.setblocking(False)
            try:
                while True:
                    more = sock.recv(1 << 20)
                    if not more:
                        break
                    data += more
            except (BlockingIOError, OSError):
                pass
            finally:
                sock.setblocking(True)
            data = leftover + data
            n = len(data) - (len(data) % 8)          # whole complex64 only
            leftover = data[n:]
            if n:
                samples = np.frombuffer(data[:n], dtype=np.complex64)
                self._write(samples)


# ─────────────────────────────────────────────────────────────────────────────
# File IQ reader — replay a recorded wire-format capture (demo / offline)
# ─────────────────────────────────────────────────────────────────────────────
class FileIQReader(_RingReader):
    """Replays a wire-format IQ capture file (as written by
    tools/capture_iq_stream.py, or `nc`-ing the IQ server to a file): a
    'SOLSDR IQ ...' header line then raw complex64. Feeds the ring buffer paced
    at the file's real sample rate so the waterfall scrolls like live, and loops
    at EOF so a demo runs indefinitely.

    Also accepts a bare .npy of complex64 (no header) via the sibling helper, or
    a headerless raw complex64 file with --file-rate/--file-freq overrides.
    """

    def __init__(self, path, ring_samples=1 << 20, loop=True,
                 rate_override=None, freq_override=None, chunk_s=0.05):
        super().__init__(ring_samples)
        self.path = path
        self.loop = loop
        self.rate_override = rate_override
        self.freq_override = freq_override
        self.chunk_s = chunk_s
        self._data_offset = 0          # byte offset where complex64 begins

    def _open_and_read_header(self):
        f = open(self.path, "rb")
        # Peek the first line; if it's a SOLSDR header, parse + consume it.
        head = f.read(256)
        rate = freq = None
        if head.startswith(b"SOLSDR"):
            nl = head.find(b"\n")
            line = head[:nl].decode("ascii", "replace").strip()
            rate, freq = self._parse_header_line(line)
            self._data_offset = nl + 1
            self.status_text = f"file · {os.path.basename(self.path)} · {line}"
        else:
            # headerless raw complex64 — need overrides
            self._data_offset = 0
            self.status_text = (f"file · {os.path.basename(self.path)} · "
                                f"raw complex64 (no header)")
        # overrides win (and cover the headerless case)
        self.rate = self.rate_override or rate or 39062.5
        self.header_freq = (self.freq_override if self.freq_override is not None
                            else (freq if freq is not None else 0.0))
        f.seek(self._data_offset)
        return f

    def run(self):
        try:
            f = self._open_and_read_header()
        except OSError as e:
            self.status_text = f"cannot open {self.path}: {e}"
            self.connected = False
            return
        self.connected = True
        chunk_samples = max(64, int(self.rate * self.chunk_s))
        chunk_bytes = chunk_samples * 8
        try:
            while self._running:
                raw = f.read(chunk_bytes)
                if not raw:
                    if self.loop:
                        f.seek(self._data_offset)   # rewind and replay
                        continue
                    break
                n = len(raw) - (len(raw) % 8)
                if n:
                    self._write(np.frombuffer(raw[:n], dtype=np.complex64))
                # pace playback to the real sample rate so the waterfall scrolls
                # like a live stream rather than blitting the whole file at once
                time.sleep(self.chunk_s)
        finally:
            try:
                f.close()
            except Exception:
                pass
            self.connected = False
            self.status_text = f"file · {os.path.basename(self.path)} · done"


# ─────────────────────────────────────────────────────────────────────────────
# Control API poller (background thread) — live radio state + control
# ─────────────────────────────────────────────────────────────────────────────
class ControlPoller(threading.Thread):
    """Polls solsdr's text control API `status` for live radio state. Reads on
    its own thread; send_command() opens a SEPARATE short-lived connection so a
    command (freq/mode/etc.) can't race the poll socket. The polled state is
    what the on-screen controls mirror, so external changes (shell, other
    clients) show up here and this panel's commands show up there."""

    def __init__(self, host, port, interval=0.5):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.interval = interval
        self._running = True
        self._lock = threading.Lock()
        self._state = {}
        self.available = False

    def send_command(self, line, timeout=2.0):
        """Send one control-API command over a fresh connection; return the
        reply string (or None on failure). Used for click-to-tune."""
        try:
            with socket.create_connection((self.host, self.port),
                                          timeout=timeout) as s:
                s.settimeout(timeout)
                s.sendall((line + "\n").encode("utf-8"))
                return self._recv_line(s)
        except OSError:
            return None

    def run(self):
        sock = None
        while self._running:
            try:
                if sock is None:
                    sock = socket.create_connection((self.host, self.port),
                                                    timeout=3)
                    sock.settimeout(3)
                sock.sendall(b"status\n")
                reply = self._recv_line(sock)
                self._parse(reply)
                self.available = True
            except OSError:
                self.available = False
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                    sock = None
            time.sleep(self.interval)

    @staticmethod
    def _recv_line(sock):
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("closed")
            buf += chunk
        return buf.split(b"\n", 1)[0].decode("utf-8", "replace").strip()

    def _parse(self, reply):
        st = {}
        for tok in reply.split()[1:]:                 # skip "OK"
            if "=" in tok:
                k, v = tok.split("=", 1)
                st[k] = v
        with self._lock:
            self._state = st

    def state(self):
        with self._lock:
            return dict(self._state)

    def stop(self):
        self._running = False


# ─────────────────────────────────────────────────────────────────────────────
# Spectrum processing
# ─────────────────────────────────────────────────────────────────────────────
class SpectrumProc:
    """Windowed FFT → dBFS, with exponential averaging and peak-hold."""

    def __init__(self, fft_size, avg=0.35):
        self.set_fft(fft_size)
        self.avg = avg                 # exponential smoothing 0<a<=1 (1=off)
        self._avg_spec = None
        self._peak = None

    def set_fft(self, n):
        self.n = int(n)
        self.win = np.blackman(self.n).astype(np.float64)
        # Normalize so a full-scale complex tone reads 0 dBFS at its bin.
        self.win_sum = np.sum(self.win)
        self._avg_spec = None
        self._peak = None

    def reset_peak(self):
        self._peak = None

    def process(self, iq, kill_dc=True):
        """iq: complex64 length >= n. Returns (spec_db, peak_db_or_None)."""
        x = iq[-self.n:].astype(np.complex64)
        spec = np.fft.fftshift(np.fft.fft(x * self.win))
        mag = np.abs(spec) / self.win_sum
        db = 20.0 * np.log10(mag + 1e-12)
        if kill_dc:
            c = self.n // 2                # center bin = DC = tuned freq
            db[c] = 0.5 * (db[c - 1] + db[c + 1])
        # exponential average
        if self._avg_spec is None or len(self._avg_spec) != self.n:
            self._avg_spec = db.copy()
        else:
            a = self.avg
            self._avg_spec = a * db + (1.0 - a) * self._avg_spec
        # peak hold
        if self._peak is None or len(self._peak) != self.n:
            self._peak = self._avg_spec.copy()
        else:
            np.maximum(self._peak, self._avg_spec, out=self._peak)
        return self._avg_spec, self._peak


# ─────────────────────────────────────────────────────────────────────────────
# Colormaps
# ─────────────────────────────────────────────────────────────────────────────
CMAP_NAMES = ["cividis", "inferno", "viridis", "magma", "plasma", "turbo",
              "CET-L9"]


def load_cmap(name):
    """Best-effort colormap load across pyqtgraph versions/sources."""
    for src in (None, "matplotlib", "colorcet"):
        try:
            cm = pg.colormap.get(name) if src is None \
                else pg.colormap.get(name, source=src)
            if cm is not None:
                return cm
        except Exception:
            continue
    return pg.colormap.get("CET-L9")     # ships with pyqtgraph


# ─────────────────────────────────────────────────────────────────────────────
# Clickable frequency dial
# ─────────────────────────────────────────────────────────────────────────────
class _Digit(QtWidgets.QLabel):
    """One frequency digit. Click the TOP half to increment that decade, the
    BOTTOM half to decrement; the mouse wheel does the same. Emits `stepped(±weight)`
    where weight is this digit's place value in Hz (e.g. 1000 for the kHz digit)."""

    # pyqtgraph.Qt normalizes `Signal` onto QtCore for every binding.
    stepped = QtCore.Signal(int)

    def __init__(self, weight):
        super().__init__("0")
        self.weight = int(weight)
        self.setAlignment(_qt("AlignmentFlag", "AlignCenter"))
        # fixed-width so the number doesn't jitter as digits change
        f = QtGui.QFont("monospace")
        f.setPointSize(20)
        f.setBold(True)
        self.setFont(f)
        self.setFixedWidth(20)
        self.setStyleSheet("QLabel{color:#111;}"
                           "QLabel:hover{color:#0a7; background:#e8f4f0;}")
        self.setCursor(QtGui.QCursor(_qt("CursorShape", "PointingHandCursor")))
        self.setToolTip(f"{self.weight:,} Hz — click top +1, bottom −1")

    def _dir(self, y):
        return +1 if y < self.height() / 2 else -1

    def mousePressEvent(self, ev):
        self.stepped.emit(self._dir(ev.pos().y()) * self.weight)

    def wheelEvent(self, ev):
        try:
            dy = ev.angleDelta().y()
        except AttributeError:
            dy = ev.delta()
        self.stepped.emit((1 if dy > 0 else -1) * self.weight)


class FrequencyDial(QtWidgets.QWidget):
    """A row of clickable per-decade digits showing the tuned frequency in Hz,
    grouped MHz.kHz.Hz with separators, plus a "0" button that zeroes everything
    below the kHz decimal (rounds to the nearest kHz). Click a digit's top to
    step that decade up by one, bottom to step down. Emits `tuned(hz)` when the
    operator changes it; call set_hz() to follow external state without emitting."""

    tuned = QtCore.Signal(int)

    # place weights from most to least significant, with '.' group separators.
    # Covers up to 999 MHz (9 digits) — plenty for the SunSDR2's HF/VHF range.
    _PLACES = [100_000_000, 10_000_000, 1_000_000, None,
               100_000, 10_000, 1_000, None,
               100, 10, 1]

    def __init__(self):
        super().__init__()
        self._hz = 0
        self._digits = {}                       # weight -> _Digit
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        for w in self._PLACES:
            if w is None:
                sep = QtWidgets.QLabel(".")
                sep.setStyleSheet("QLabel{color:#888;}")
                lay.addWidget(sep)
                continue
            d = _Digit(w)
            d.stepped.connect(self._on_step)
            self._digits[w] = d
            lay.addWidget(d)
        lay.addSpacing(4)
        lay.addWidget(QtWidgets.QLabel(
            "<span style='color:#888;font-size:11px'>Hz</span>"))
        z = QtWidgets.QPushButton("0")
        z.setFixedWidth(24)
        z.setToolTip("Zero all digits below the kHz decimal "
                     "(round to the nearest kHz)")
        z.clicked.connect(self._zeroize)
        lay.addSpacing(6)
        lay.addWidget(z)

    def _render(self):
        # 9 digit places (the None entries are '.' separators, not digits).
        s = f"{self._hz:09d}"
        for ch, w in zip(s, [w for w in self._PLACES if w is not None]):
            self._digits[w].setText(ch)

    def set_hz(self, hz):
        """Follow external state — updates the display WITHOUT emitting tuned()."""
        hz = max(0, int(round(hz)))
        if hz != self._hz:
            self._hz = hz
            self._render()

    def _on_step(self, delta):
        self._hz = max(0, self._hz + int(delta))
        self._render()
        self.tuned.emit(self._hz)

    def _zeroize(self):
        self._hz = int(round(self._hz / 1000.0)) * 1000
        self._render()
        self.tuned.emit(self._hz)


# ─────────────────────────────────────────────────────────────────────────────
# Main window
# ─────────────────────────────────────────────────────────────────────────────
class Panadapter(QtWidgets.QMainWindow):
    def __init__(self, iq: IQReader, ctrl: ControlPoller, *,
                 fft_size=4096, ref_offset=0.0, wf_history=400,
                 update_ms=33, rescale_interval=5.0, pretty=False):
        super().__init__()
        self.iq = iq
        self.ctrl = ctrl
        # Control is enabled whenever we have a control-API connection. A
        # left-click on the spectrum/waterfall tunes the radio; the toolbar
        # mode/frequency widgets command it too. All mirror the radio's live
        # state via the poll, so shell/other-client changes show up here.
        self.can_control = ctrl is not None and hasattr(ctrl, 'send_command')
        # Radio-control widgets exist only when the control API is reachable
        # (rows 1-2 are skipped otherwise); default them to None so display-only
        # code paths and _sync_controls guards work without them.
        self.cmb_mode = None
        self.freq_dial = None
        self.cmb_sharp = None
        # "pretty" = antialiased, filled, thicker trace (nice on a GPU/fast box).
        # Default is "fast": no antialias, thin trace, no fill — the software
        # rasterizer spends ~50 ms/frame on AA+fill of a 2048-point curve, vs
        # ~1.5 ms without, i.e. ~9 fps -> well past 30 fps on a CPU-only box.
        self.pretty = bool(pretty)
        self.ref_offset = ref_offset      # dBFS -> dBm; 0 => axis reads dBFS
        self.unit = "dBm" if ref_offset else "dBFS"
        self.proc = SpectrumProc(fft_size)
        self.wf_history = wf_history

        # display state
        self.autoscale = True
        self.freeze = False
        self.peak_hold = False
        self.kill_dc = True
        # mirror the display spectrum to put USB above (right of) the dial — see
        # the conjugation note in _tick. Default on for the SunSDR2 wire format.
        self.spectrum_invert = True
        self.ref_level = -20.0            # fixed-mode top of scale (display units)
        self.dyn_range = 90.0             # fixed-mode span in dB
        self._auto_min = -120.0
        self._auto_max = -20.0
        # last range actually pushed to the plot — so we only re-apply (and pay
        # the axis-relayout repaint) when it truly changes (see _tick).
        self._applied_yrange = (None, None)
        self._applied_xrange = (None, None)
        # frequency zoom: the +/- buttons show a fraction of the full span,
        # centered on the tuned frequency. 1.0 = full span; smaller = zoomed in.
        self.zoom = 1.0
        self._zoom_min = 0.02            # ~50x max zoom-in
        self._zoom_center_hz = None      # None => follow the tuned center
        # autoscale recomputes the Y range on this cadence (seconds), not every
        # frame — the per-frame setYRange repaint is the main CPU cost.
        self.rescale_interval = float(rescale_interval)
        self._last_rescale = 0.0
        self.cmap_name = "cividis"

        # axis geometry (updated live)
        self.center_hz = iq.header_freq or 0.0
        self.rate = iq.rate or 39062.5
        self._wf = None                   # waterfall buffer [time, freq]
        self._wf_n = None

        self.setWindowTitle("solsdr panadapter")
        self.resize(1100, 720)
        self._build_ui()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(update_ms)

    # -- UI ------------------------------------------------------------------
    def _build_ui(self):
        pg.setConfigOptions(antialias=self.pretty, background="#101418",
                            foreground="#c8d0d8")
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        vbox = QtWidgets.QVBoxLayout(central)
        vbox.setContentsMargins(6, 4, 6, 4)
        vbox.setSpacing(4)

        # Toolbar rows, grouped by function:
        #   1. Radio  — tuning + receive filter (mode, freq, BW, skirt, preamp)
        #   2. RX DSP — signal conditioning (AGC, gain, RIT, NR/NB/notch/APF/SQL)
        #   3. Display — panadapter view only (scale, zoom, FFT, colors, ...)
        # Rows 1-2 need the control API; row 3 is always shown.
        if self.can_control:
            vbox.addLayout(self._build_radio_controls())
            vbox.addLayout(self._build_dsp_controls())
        vbox.addLayout(self._build_display_controls())

        # info bar
        self.info = QtWidgets.QLabel("—")
        self.info.setTextFormat(RICHTEXT)
        self.info.setStyleSheet("QLabel{color:#000000;font-size:12px;"
                                "padding:2px 4px;}")
        vbox.addWidget(self.info)

        # spectrum + waterfall in a draggable splitter
        splitter = QtWidgets.QSplitter(VERTICAL)
        vbox.addWidget(splitter, 1)

        # spectrum plot (custom bottom axis with tenths minor ticks)
        self.gl_spec = pg.GraphicsLayoutWidget()
        self.p_spec = self.gl_spec.addPlot(
            axisItems={"bottom": TenthsAxisItem(orientation="bottom")})
        self.p_spec.showGrid(x=True, y=True, alpha=0.25)
        self.p_spec.setLabel("left", "level", units=self.unit)
        self.p_spec.setLabel("bottom", "frequency", units="Hz")
        self.p_spec.setMouseEnabled(x=True, y=False)
        self.p_spec.setMenuEnabled(False)
        if self.pretty:
            self.curve = self.p_spec.plot(pen=pg.mkPen("#39d3ff", width=1.4),
                                          fillLevel=-200,
                                          brush=pg.mkBrush(57, 211, 255, 45))
        else:
            # fast: thin, non-antialiased trace, no fill (see __init__ note)
            self.curve = self.p_spec.plot(
                pen=pg.mkPen("#39d3ff", width=1), antialias=False)
        self.peak_curve = self.p_spec.plot(
            pen=pg.mkPen("#ff9f1c", width=1.0, style=DASHLINE))
        self.peak_curve.setVisible(False)
        splitter.addWidget(self.gl_spec)

        # waterfall plot (shares the spectrum's x-axis; same tenths axis)
        self.gl_wf = pg.GraphicsLayoutWidget()
        self.p_wf = self.gl_wf.addPlot(
            axisItems={"bottom": TenthsAxisItem(orientation="bottom")})
        self.p_wf.setLabel("bottom", "frequency", units="Hz")
        self.p_wf.setLabel("left", "time")
        self.p_wf.getAxis("left").setStyle(showValues=False)
        self.p_wf.setMouseEnabled(x=True, y=False)
        self.p_wf.setMenuEnabled(False)
        self.p_wf.invertY(True)           # newest row at the top
        self.img = pg.ImageItem(axisOrder="row-major")
        self.p_wf.addItem(self.img)
        self._apply_cmap()
        splitter.addWidget(self.gl_wf)
        splitter.setSizes([320, 380])

        # link x-axes so spectrum and waterfall stay aligned on zoom/pan
        self.p_wf.setXLink(self.p_spec)

        # crosshair
        self.vline = pg.InfiniteLine(angle=90, movable=False,
                                     pen=pg.mkPen("#ffffff", width=0.6,
                                                  style=DOTLINE))
        self.hline = pg.InfiniteLine(angle=0, movable=False,
                                     pen=pg.mkPen("#ffffff", width=0.6,
                                                  style=DOTLINE))
        self.vline_wf = pg.InfiniteLine(angle=90, movable=False,
                                        pen=pg.mkPen("#ffffff", width=0.6,
                                                     style=DOTLINE))
        self.p_spec.addItem(self.vline, ignoreBounds=True)
        self.p_spec.addItem(self.hline, ignoreBounds=True)
        self.p_wf.addItem(self.vline_wf, ignoreBounds=True)

        # -- filter passband overlay + dial-frequency line --
        # A translucent region shows the receive filter passband (dial+lo ..
        # dial+hi). It's draggable on the SPECTRUM (edges send `filter <lo> <hi>`
        # as RF offsets) and mirrored, non-interactive, on the waterfall. A solid
        # vertical line marks the exact dial (tuned) frequency on both.
        self._filter_lo_hz = None          # RF offsets from dial (Hz); None until known
        self._filter_hi_hz = None
        self._filter_syncing = False       # guard: setRegion shouldn't send a command
        # translucent fill + solid bright edges so it reads even over a dark,
        # near-empty spectrum. Edges are grab handles on the spectrum.
        fbrush = pg.mkBrush(90, 205, 255, 60)
        fedge = pg.mkPen(120, 220, 255, 220, width=2)
        fhover = pg.mkPen(180, 240, 255, 255, width=3)
        self.filter_region = pg.LinearRegionItem(
            values=(0, 1), brush=fbrush, pen=fedge, hoverPen=fhover,
            movable=self.can_control)
        self.filter_region.setZValue(-20)
        self.filter_region_wf = pg.LinearRegionItem(
            values=(0, 1), brush=fbrush, pen=fedge, movable=False)
        self.filter_region_wf.setZValue(10)
        dialpen = pg.mkPen("#ffd21c", width=1.2)
        self.dial_line = pg.InfiniteLine(angle=90, movable=False, pen=dialpen)
        self.dial_line_wf = pg.InfiniteLine(angle=90, movable=False, pen=dialpen)
        # hidden until the control API reports a real passband (filter_lo/hi);
        # otherwise a stray band would sit at 0 Hz in display-only mode.
        self.filter_region.setVisible(False)
        self.filter_region_wf.setVisible(False)
        self.p_spec.addItem(self.filter_region, ignoreBounds=True)
        self.p_spec.addItem(self.dial_line, ignoreBounds=True)
        self.p_wf.addItem(self.filter_region_wf, ignoreBounds=True)
        self.p_wf.addItem(self.dial_line_wf, ignoreBounds=True)
        if self.can_control:
            self.filter_region.sigRegionChangeFinished.connect(
                self._on_filter_drag)
        self._mouse_proxy = pg.SignalProxy(self.p_spec.scene().sigMouseMoved,
                                           rateLimit=60, slot=self._on_mouse)
        # Click-to-tune: send the radio to the frequency under the click (on the
        # spectrum and waterfall, which share the x-axis). Enabled when we can
        # control the radio.
        if self.can_control:
            self.p_spec.scene().sigMouseClicked.connect(self._on_click)
            self.p_wf.scene().sigMouseClicked.connect(self._on_click)

        # cursor readout
        self.cursor_lbl = QtWidgets.QLabel("cursor: —")
        self.cursor_lbl.setStyleSheet("QLabel{color:#9fb3c8;font-size:12px;}")
        vbox.addWidget(self.cursor_lbl)

    @staticmethod
    def _group_sep(row):
        """Add a vertical separator between logical groups within a toolbar row."""
        sep = QtWidgets.QFrame()
        sep.setFrameShape(_qt_frameshape("VLine"))
        sep.setStyleSheet("color:#ccc;")
        row.addWidget(sep)

    @staticmethod
    def _group_label(row, text):
        """Add a small bold group heading label within a toolbar row."""
        lbl = QtWidgets.QLabel(text)
        lbl.setStyleSheet("QLabel{color:#556;font-weight:bold;font-size:11px;}")
        row.addWidget(lbl)

    # -- row 1: radio tuning + receive filter --------------------------------
    def _build_radio_controls(self):
        """Row 1 — what you're RECEIVING: mode, frequency dial, filter bandwidth
        presets, skirt sharpness, and preamp. All mirror the control-API status
        both ways. Built only when the control API is reachable."""
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)

        # mode + clickable frequency dial
        self.cmb_mode = QtWidgets.QComboBox()
        self.cmb_mode.addItems(CONTROL_MODES)
        self.cmb_mode.setToolTip("Radio mode (mirrors the radio; changing it "
                                 "commands the radio)")
        self.cmb_mode.activated.connect(self._on_mode_pick)
        row.addWidget(QtWidgets.QLabel("Mode"))
        row.addWidget(self.cmb_mode)

        self.freq_dial = FrequencyDial()
        self.freq_dial.tuned.connect(self._on_dial_tuned)
        row.addWidget(self.freq_dial)

        self._group_sep(row)

        # filter bandwidth presets (mode-aware) + custom
        row.addWidget(QtWidgets.QLabel("BW"))
        self._bw_layout = QtWidgets.QHBoxLayout()
        self._bw_layout.setSpacing(2)
        self._bw_buttons = []
        self._bw_mode = None
        row.addLayout(self._bw_layout)
        self.btn_bw_custom = QtWidgets.QPushButton("Custom")
        self.btn_bw_custom.setFixedWidth(56)
        self.btn_bw_custom.setToolTip("Set an exact filter bandwidth in Hz")
        self.btn_bw_custom.clicked.connect(self._on_bw_custom)
        row.addWidget(self.btn_bw_custom)
        self._rebuild_bw_presets("USB")

        # filter skirt sharpness
        row.addWidget(QtWidgets.QLabel("Skirt"))
        self.cmb_sharp = QtWidgets.QComboBox()
        self.cmb_sharp.addItems(SHARPNESS_CHOICES)
        self.cmb_sharp.setToolTip("SSB filter skirt sharpness: sharper = steeper "
                                  "rolloff outside the passband, more latency")
        self.cmb_sharp.activated.connect(self._on_sharp_pick)
        row.addWidget(self.cmb_sharp)

        self._group_sep(row)

        # preamp / attenuator
        row.addWidget(QtWidgets.QLabel("Preamp"))
        self.cmb_preamp = QtWidgets.QComboBox()
        for lbl in ("-20", "-10", "0", "+10"):
            self.cmb_preamp.addItem(lbl)
        self.cmb_preamp.setToolTip("RX preamp / attenuator (dB)")
        self.cmb_preamp.activated.connect(self._on_preamp_pick)
        row.addWidget(self.cmb_preamp)

        row.addStretch(1)
        return row

    # -- row 3: display / view controls (no radio state) ---------------------
    def _build_display_controls(self):
        """Row 3 — panadapter VIEW only (never touches the radio): scale, zoom,
        FFT size, averaging, peak-hold, DC, colormap, freeze. Always shown."""
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)

        # amplitude scaling
        self.cb_auto = QtWidgets.QCheckBox("Auto-scale")
        self.cb_auto.setChecked(True)
        self.cb_auto.toggled.connect(self._on_auto)
        row.addWidget(self.cb_auto)

        self.btn_rescale = QtWidgets.QPushButton("Rescale")
        self.btn_rescale.setToolTip("Recompute the auto-scale range now "
                                    "(auto-rescale runs every few seconds)")
        self.btn_rescale.clicked.connect(self.rescale_now)
        row.addWidget(self.btn_rescale)

        row.addWidget(QtWidgets.QLabel("Ref"))
        self.sp_ref = QtWidgets.QDoubleSpinBox()
        self.sp_ref.setRange(-200, 60)
        self.sp_ref.setValue(self.ref_level)
        self.sp_ref.setSuffix(f" {self.unit}")
        self.sp_ref.setEnabled(False)
        self.sp_ref.valueChanged.connect(self._on_ref)
        row.addWidget(self.sp_ref)

        row.addWidget(QtWidgets.QLabel("Range"))
        self.sp_range = QtWidgets.QDoubleSpinBox()
        self.sp_range.setRange(10, 160)
        self.sp_range.setValue(self.dyn_range)
        self.sp_range.setSuffix(" dB")
        self.sp_range.setEnabled(False)
        self.sp_range.valueChanged.connect(self._on_range)
        row.addWidget(self.sp_range)

        self._group_sep(row)

        # frequency zoom
        row.addWidget(QtWidgets.QLabel("Zoom"))
        self.btn_zoom_out = QtWidgets.QPushButton("−")   # minus sign
        self.btn_zoom_out.setToolTip("Zoom out (show more of the band)")
        self.btn_zoom_out.setFixedWidth(30)
        self.btn_zoom_out.clicked.connect(lambda: self.zoom_by(2.0))
        row.addWidget(self.btn_zoom_out)
        self.btn_zoom_in = QtWidgets.QPushButton("+")
        self.btn_zoom_in.setToolTip("Zoom in (narrower span around the tuned freq)")
        self.btn_zoom_in.setFixedWidth(30)
        self.btn_zoom_in.clicked.connect(lambda: self.zoom_by(0.5))
        row.addWidget(self.btn_zoom_in)
        self.btn_zoom_full = QtWidgets.QPushButton("Full")
        self.btn_zoom_full.setToolTip("Reset to the full IQ span")
        self.btn_zoom_full.clicked.connect(self.zoom_full)
        row.addWidget(self.btn_zoom_full)

        self._group_sep(row)

        # FFT / averaging
        row.addWidget(QtWidgets.QLabel("FFT"))
        self.cmb_fft = QtWidgets.QComboBox()
        for n in (512, 1024, 2048, 4096, 8192):
            self.cmb_fft.addItem(str(n), n)
        self.cmb_fft.setCurrentText("4096")
        self.cmb_fft.currentIndexChanged.connect(self._on_fft)
        row.addWidget(self.cmb_fft)

        row.addWidget(QtWidgets.QLabel("Avg"))
        self.sl_avg = QtWidgets.QSlider(HORIZONTAL)
        self.sl_avg.setRange(1, 100)
        self.sl_avg.setValue(35)
        self.sl_avg.setFixedWidth(90)
        self.sl_avg.valueChanged.connect(self._on_avg)
        row.addWidget(self.sl_avg)

        self._group_sep(row)

        # trace options
        self.cb_peak = QtWidgets.QCheckBox("Peak hold")
        self.cb_peak.toggled.connect(self._on_peak)
        row.addWidget(self.cb_peak)
        self.btn_clrpk = QtWidgets.QPushButton("Clear pk")
        self.btn_clrpk.clicked.connect(lambda: self.proc.reset_peak())
        row.addWidget(self.btn_clrpk)

        self.cb_dc = QtWidgets.QCheckBox("Hide DC")
        self.cb_dc.setChecked(True)
        self.cb_dc.toggled.connect(self._on_dc)
        row.addWidget(self.cb_dc)

        row.addWidget(QtWidgets.QLabel("Colors"))
        self.cmb_cmap = QtWidgets.QComboBox()
        self.cmb_cmap.addItems(CMAP_NAMES)
        self.cmb_cmap.currentTextChanged.connect(self._on_cmap)
        row.addWidget(self.cmb_cmap)

        self.cb_freeze = QtWidgets.QCheckBox("Freeze")
        self.cb_freeze.toggled.connect(lambda v: setattr(self, "freeze", v))
        row.addWidget(self.cb_freeze)

        row.addStretch(1)
        return row

    # -- row 2: RX DSP signal conditioning -----------------------------------
    def _build_dsp_controls(self):
        """Row 2 — RX DSP signal conditioning: AGC/gain, RIT, and the noise/notch
        stages (NR, NB, notch, APF, squelch). Each mirrors the live control-API
        `status`, so a shell/other-client change moves the widget here and moving
        it here commands the radio. (Backend fields: see _sync_dsp_controls.)"""
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        # guard flag: True while we're pushing polled state INTO a widget, so the
        # widget's change signal doesn't bounce back as a command (feedback loop).
        self._dsp_syncing = False
        self._dsp_labels = {}          # key -> value QLabel

        # --- gain / AGC / RIT ---
        row.addWidget(QtWidgets.QLabel("AGC"))
        self.cmb_agc = QtWidgets.QComboBox()
        self.cmb_agc.addItems(["auto", "on", "off"])
        self.cmb_agc.setToolTip("AGC mode (auto/on/off). Manual gain sets a "
                                "fixed gain, which shows here as 'off'.")
        self.cmb_agc.activated.connect(self._on_agc_pick)
        row.addWidget(self.cmb_agc)

        self._add_dsp_slider(row, "Gain", "gain", lo=100, hi=50000, step=100,
                             tip="Fixed audio gain (turns AGC off)")
        self._add_dsp_slider(row, "RIT", "rit", lo=-2000, hi=2000, step=10,
                             suffix=" Hz", tip="Receiver incremental tuning")

        self._group_sep(row)

        # --- noise / interference reduction ---
        for label, key, tip in (
                ("NR", "nr", "Noise reduction (0 = off)"),
                ("NB", "nb", "Noise blanker (0 = off)"),
                ("APF", "apf", "Audio peak filter (0 = off)"),
                ("SQL", "squelch", "Squelch level (0 = open)")):
            self._add_dsp_slider(row, label, key, lo=0, hi=100, step=1,
                                 scale=0.01, tip=tip)
        self._add_dsp_slider(row, "Notch", "notch", lo=0, hi=5000, step=10,
                             suffix=" Hz", tip="Notch filter frequency (0 = off)")

        row.addStretch(1)
        return row

    def _add_dsp_slider(self, row, label, key, *, lo, hi, step=1, scale=1.0,
                        suffix="", tip=""):
        """Add a labeled horizontal slider that commands the control API `key`.
        Slider integers map to command values via `scale` (value = slider*scale).
        Stashes the widget as self.sl_<key> and a value QLabel in _dsp_labels."""
        row.addWidget(QtWidgets.QLabel(label))
        sl = QtWidgets.QSlider(HORIZONTAL)
        sl.setRange(int(lo), int(hi))
        sl.setSingleStep(int(step))
        sl.setFixedWidth(80)
        if tip:
            sl.setToolTip(tip)
        sl._scale = scale
        sl._suffix = suffix
        sl._key = key
        sl.valueChanged.connect(lambda v, k=key: self._on_dsp_slider(k, v))
        setattr(self, f"sl_{key}", sl)
        row.addWidget(sl)
        val = QtWidgets.QLabel("—")
        val.setStyleSheet("QLabel{color:#333;font-size:11px;}")
        val.setFixedWidth(46)
        self._dsp_labels[key] = val
        row.addWidget(val)

    def _fmt_dsp_value(self, sl, value):
        v = value * sl._scale
        if sl._scale < 1.0:
            return f"{v:.2f}{sl._suffix}"
        return f"{int(round(v))}{sl._suffix}"

    def _on_dsp_slider(self, key, value):
        sl = getattr(self, f"sl_{key}")
        self._dsp_labels[key].setText(self._fmt_dsp_value(sl, value))
        if self._dsp_syncing:
            return                        # programmatic update — don't command
        cmdval = value * sl._scale
        arg = f"{cmdval:.3f}" if sl._scale < 1.0 else f"{int(round(cmdval))}"
        reply = self.ctrl.send_command(f"{key} {arg}")
        if not (reply and reply.startswith("OK")):
            self.cursor_lbl.setText(f"{key} failed: {reply or 'no reply'}")

    def _on_agc_pick(self, _idx):
        mode = self.cmb_agc.currentText()
        reply = self.ctrl.send_command(f"agc {mode}")
        if not (reply and reply.startswith("OK")):
            self.cursor_lbl.setText(f"agc failed: {reply or 'no reply'}")

    def _on_preamp_pick(self, _idx):
        state = self.cmb_preamp.currentText()
        reply = self.ctrl.send_command(f"preamp {state}")
        if not (reply and reply.startswith("OK")):
            self.cursor_lbl.setText(f"preamp failed: {reply or 'no reply'}")

    def _on_sharp_pick(self, _idx):
        s = self.cmb_sharp.currentText()
        reply = self.ctrl.send_command(f"sharpness {s}")
        if reply and reply.startswith("OK"):
            self.cursor_lbl.setText(f"filter skirt -> {s}")
        else:
            self.cursor_lbl.setText(f"sharpness failed: {reply or 'no reply'}")

    def _sync_dsp_controls(self, st):
        """Mirror the polled `status` DSP fields into the second-row widgets
        WITHOUT emitting commands. Skips a widget whose dropdown is open or whose
        slider the user is dragging, so we don't fight active edits."""
        self._dsp_syncing = True
        try:
            # AGC dropdown: 'auto'/'on'/'off'; a 'fixed:...' mode reads as 'off'.
            agc = st.get("agc")
            if agc is not None and not self.cmb_agc.view().isVisible():
                shown = "off" if str(agc).startswith("fixed:") else str(agc)
                if shown in ("auto", "on", "off") and self.cmb_agc.currentText() != shown:
                    self.cmb_agc.setCurrentText(shown)
            # sliders
            for key in ("gain", "rit", "nr", "nb", "apf", "squelch", "notch"):
                sl = getattr(self, f"sl_{key}", None)
                if sl is None or sl.isSliderDown():
                    continue
                raw = st.get(key)
                if raw is None or raw in ("None", ""):
                    continue
                try:
                    want = int(round(float(raw) / sl._scale))
                except ValueError:
                    continue
                want = max(sl.minimum(), min(sl.maximum(), want))
                if sl.value() != want:
                    sl.setValue(want)
                else:
                    # value unchanged but label may be "—" on first sync
                    self._dsp_labels[key].setText(self._fmt_dsp_value(sl, want))
            # preamp dropdown
            pa = st.get("preamp")
            if (pa is not None and pa not in ("None", "")
                    and not self.cmb_preamp.view().isVisible()):
                if self.cmb_preamp.findText(str(pa)) >= 0 \
                        and self.cmb_preamp.currentText() != str(pa):
                    self.cmb_preamp.setCurrentText(str(pa))
            # sharpness dropdown
            sh = st.get("sharpness")
            if (self.cmb_sharp is not None and sh is not None
                    and sh in SHARPNESS_CHOICES
                    and not self.cmb_sharp.view().isVisible()
                    and self.cmb_sharp.currentText() != sh):
                self.cmb_sharp.setCurrentText(sh)
        finally:
            self._dsp_syncing = False

    # -- filter passband overlay --------------------------------------------
    def _update_filter_overlay(self, st):
        """Position the filter region + dial line from the polled status. The
        region is drawn in ABSOLUTE Hz (dial+offset); status carries RF offsets
        (filter_lo/filter_hi). Also refreshes the preset-button highlight."""
        # dial line at the exact tuned frequency
        if self.center_hz:
            self.dial_line.setPos(self.center_hz)
            self.dial_line_wf.setPos(self.center_hz)
        lo = st.get("filter_lo")
        hi = st.get("filter_hi")
        if lo is None or hi is None or lo in ("None", "") or hi in ("None", ""):
            # control API is up but not reporting the passband — the radio
            # process is almost certainly running OLDER code (restart solsdr to
            # pick up the filter_lo/filter_hi status fields). Note it once.
            if not getattr(self, "_warned_no_filter", False):
                self._warned_no_filter = True
                self.cursor_lbl.setText(
                    "filter overlay: radio isn't reporting filter_lo/hi — "
                    "restart solsdr to enable the passband display")
            return
        self._warned_no_filter = False
        try:
            lo = float(lo); hi = float(hi)
        except ValueError:
            return
        self._filter_lo_hz, self._filter_hi_hz = lo, hi
        abs_lo = self.center_hz + lo
        abs_hi = self.center_hz + hi
        # push into both regions without triggering the drag-finished handler
        self._filter_syncing = True
        try:
            self.filter_region.setRegion((abs_lo, abs_hi))
            self.filter_region_wf.setRegion((abs_lo, abs_hi))
            if not self.filter_region.isVisible():
                self.filter_region.setVisible(True)
                self.filter_region_wf.setVisible(True)
        finally:
            self._filter_syncing = False
        if hasattr(self, "_bw_buttons"):
            self._highlight_bw_preset(hi - lo)

    def _on_filter_drag(self):
        """User dragged a filter-region edge on the spectrum. Convert the
        absolute-Hz edges back to RF offsets from the dial and command the
        radio. Ignored while we're programmatically syncing the region."""
        if self._filter_syncing or not self.can_control:
            return
        a, b = self.filter_region.getRegion()
        lo = int(round(a - self.center_hz))
        hi = int(round(b - self.center_hz))
        if hi < lo:
            lo, hi = hi, lo
        reply = self.ctrl.send_command(f"filter {lo} {hi}")
        if reply and reply.startswith("OK"):
            self._filter_lo_hz, self._filter_hi_hz = lo, hi
            self.filter_region_wf.setRegion((self.center_hz + lo,
                                             self.center_hz + hi))
            self.cursor_lbl.setText(f"filter -> {lo}..{hi} Hz")
        else:
            self.cursor_lbl.setText(f"filter failed: {reply or 'no reply'}")

    # -- bandwidth presets ---------------------------------------------------
    def _apply_bandwidth(self, width_hz):
        """Set the passband to `width_hz` wide, preserving the current mode's
        placement (USB above the dial, LSB below, CW centered on 0). Commands
        the radio via the control API."""
        if not self.can_control:
            return
        mode = self.cmb_mode.currentText() if self.cmb_mode else "USB"
        w = float(width_hz)
        edge = 100.0                           # SSB inner (low) edge, matches demod
        if mode == "LSB":
            lo, hi = -(edge + w), -edge        # keep the SSB inner edge
        elif mode in ("CW", "CWU", "CWL"):
            lo, hi = -w / 2.0, w / 2.0
        elif mode in ("AM", "FM"):
            lo, hi = -w / 2.0, w / 2.0
        else:                                  # USB and anything else
            lo, hi = edge, edge + w
        lo, hi = int(round(lo)), int(round(hi))
        reply = self.ctrl.send_command(f"filter {lo} {hi}")
        if reply and reply.startswith("OK"):
            self.cursor_lbl.setText(f"BW {int(w)} Hz -> filter {lo}..{hi}")
        else:
            self.cursor_lbl.setText(f"BW set failed: {reply or 'no reply'}")

    def _bw_presets_for_mode(self, mode):
        """(label, width_hz) presets appropriate to a mode."""
        if mode in ("CW", "CWU", "CWL"):
            return [("200", 200), ("400", 400), ("600", 600)]
        # SSB / AM / FM voice-ish
        return [("2.4k", 2400), ("2.7k", 2700), ("3.0k", 3000)]

    def _rebuild_bw_presets(self, mode):
        """Rebuild the preset buttons for the current mode (CW vs SSB widths)."""
        if not hasattr(self, "_bw_layout"):
            return
        # clear existing preset buttons
        for b in getattr(self, "_bw_buttons", []):
            self._bw_layout.removeWidget(b)
            b.deleteLater()
        self._bw_buttons = []
        for label, width in self._bw_presets_for_mode(mode):
            btn = QtWidgets.QPushButton(label)
            btn.setFixedWidth(42)
            btn.setCheckable(True)
            btn.setToolTip(f"Set filter bandwidth to {label} "
                           f"({width} Hz)")
            btn.clicked.connect(lambda _=False, w=width: self._apply_bandwidth(w))
            btn._bw_width = width
            self._bw_layout.addWidget(btn)
            self._bw_buttons.append(btn)
        self._bw_mode = mode

    def _on_bw_custom(self):
        """Prompt for an exact filter bandwidth (Hz) and apply it."""
        cur = 0
        if self._filter_lo_hz is not None and self._filter_hi_hz is not None:
            cur = int(round(self._filter_hi_hz - self._filter_lo_hz))
        w, ok = QtWidgets.QInputDialog.getInt(
            self, "Custom bandwidth", "Filter bandwidth (Hz):",
            value=max(cur, 100), min=50, max=20000, step=50)
        if ok:
            self._apply_bandwidth(w)

    def _highlight_bw_preset(self, width_hz):
        """Check the preset button matching the current width (within 1 Hz),
        uncheck the rest. The custom drag/shell width just leaves none checked."""
        for b in getattr(self, "_bw_buttons", []):
            b.setChecked(abs(b._bw_width - width_hz) < 1.0)

    # -- control callbacks ---------------------------------------------------
    def _on_auto(self, v):
        self.autoscale = v
        self.sp_ref.setEnabled(not v)
        self.sp_range.setEnabled(not v)
        self.rescale_now()            # snap immediately when toggled

    def rescale_now(self):
        """Force autoscale to recompute the range on the next frame."""
        self._last_rescale = 0.0

    # -- frequency zoom ------------------------------------------------------
    def zoom_by(self, factor):
        """Multiply the visible frequency span by `factor` (<1 zooms in,
        >1 zooms out), centered on the tuned frequency. Clamped to [zoom_min, 1]."""
        self.zoom = max(self._zoom_min, min(1.0, self.zoom * factor))
        self._apply_zoom()

    def zoom_full(self):
        """Reset to the full IQ span."""
        self.zoom = 1.0
        self._apply_zoom()

    def _apply_zoom(self):
        """Set the spectrum x-range to the zoomed window and mark it applied so
        _tick doesn't fight it. Waterfall follows via the linked x-axis."""
        half = (self.rate / 2.0) * self.zoom
        c = self._zoom_center_hz if self._zoom_center_hz is not None else self.center_hz
        lo, hi = c - half, c + half
        self._applied_xrange = (lo, hi)
        self.p_spec.setXRange(lo, hi, padding=0)

    def _on_ref(self, v):
        self.ref_level = v

    def _on_range(self, v):
        self.dyn_range = v

    def _on_fft(self):
        self.proc.set_fft(self.cmb_fft.currentData())
        self._wf = None                   # width changed → rebuild waterfall

    def _on_avg(self, v):
        self.proc.avg = v / 100.0

    def _on_peak(self, v):
        self.peak_hold = v
        self.peak_curve.setVisible(v)
        if v:
            self.proc.reset_peak()

    def _on_dc(self, v):
        self.kill_dc = v

    def _on_cmap(self, name):
        self.cmap_name = name
        self._apply_cmap()

    def _apply_cmap(self):
        cm = load_cmap(self.cmap_name)
        self.img.setColorMap(cm)

    # -- mouse ---------------------------------------------------------------
    def _on_mouse(self, evt):
        pos = evt[0]
        vb = self.p_spec.vb
        if not self.p_spec.sceneBoundingRect().contains(pos):
            # maybe over the waterfall
            if self.p_wf.sceneBoundingRect().contains(pos):
                mp = self.p_wf.vb.mapSceneToView(pos)
                self._set_cursor(mp.x(), None)
            return
        mp = vb.mapSceneToView(pos)
        self._set_cursor(mp.x(), mp.y())

    def _on_click(self, ev):
        """Click-to-tune: map the click x-position to a frequency and command
        the radio there via the control API. Left-click only; ignores clicks
        outside the plot. Needs the control API (--control-api on the radio)."""
        try:
            from pyqtgraph.Qt import QtCore
            if ev.button() != QtCore.Qt.MouseButton.LeftButton:
                return
        except Exception:
            pass
        pos = ev.scenePos()
        if self.p_spec.sceneBoundingRect().contains(pos):
            freq = self.p_spec.vb.mapSceneToView(pos).x()
        elif self.p_wf.sceneBoundingRect().contains(pos):
            freq = self.p_wf.vb.mapSceneToView(pos).x()
        else:
            return
        self._tune_to(freq, label="click")

    # -- radio control widgets ----------------------------------------------
    def _tune_to(self, freq_hz, label=None):
        """Command the radio to freq_hz via the control API and re-center the
        display. Shared by click-to-tune and the frequency dial."""
        freq_hz = int(round(freq_hz))
        if not self.can_control:
            self.cursor_lbl.setText("tuning needs the control API "
                                    "(start solsdr with the control API on)")
            return False
        reply = self.ctrl.send_command(f"freq {freq_hz}")
        if reply and reply.startswith("OK"):
            self.center_hz = float(freq_hz)     # re-center immediately
            self._zoom_center_hz = None
            if self.freq_dial is not None:
                self.freq_dial.set_hz(freq_hz)
            self.cursor_lbl.setText(
                f"{label or 'tuned'} -> {freq_hz/1e6:.5f} MHz")
            return True
        self.cursor_lbl.setText(f"tune failed: {reply or 'no reply'}")
        return False

    def _on_dial_tuned(self, freq_hz):
        """Frequency dial digit clicked / zeroized."""
        self._tune_to(freq_hz, label="dial")

    def _on_mode_pick(self, _idx):
        """Mode dropdown changed by the operator."""
        mode = self.cmb_mode.currentText()
        reply = self.ctrl.send_command(f"mode {mode}")
        if reply and reply.startswith("OK"):
            self.cursor_lbl.setText(f"mode -> {mode}")
        else:
            self.cursor_lbl.setText(f"mode set failed: {reply or 'no reply'}")

    def _sync_controls(self):
        """Mirror the live polled radio state into the control widgets WITHOUT
        firing their change signals — so external changes (shell, other clients)
        are reflected here. Skips a widget while the user is actively editing it
        (dropdown popup open) to avoid yanking it out from under them."""
        # dial-frequency line tracks the tuned center even without control (it's
        # informational, not interactive).
        if hasattr(self, "dial_line") and self.center_hz:
            self.dial_line.setPos(self.center_hz)
            self.dial_line_wf.setPos(self.center_hz)
        if not self.can_control:
            return
        st = self.ctrl.state()
        # frequency dial follows center_hz (already updated from status)
        if self.freq_dial is not None and self.center_hz:
            self.freq_dial.set_hz(self.center_hz)
        # mode dropdown follows status; setCurrentText doesn't emit activated()
        # (that's user-only), so this won't loop back into a command.
        m = st.get("mode")
        if (self.cmb_mode is not None and m and m in CONTROL_MODES
                and not self.cmb_mode.view().isVisible()
                and self.cmb_mode.currentText() != m):
            self.cmb_mode.setCurrentText(m)
        # second-row DSP controls (present only when built)
        if hasattr(self, "cmb_agc"):
            self._sync_dsp_controls(st)
        # filter passband overlay + dial line + preset highlight
        if hasattr(self, "filter_region"):
            self._update_filter_overlay(st)
        # rebuild BW presets if the mode changed (CW widths vs SSB widths)
        m = st.get("mode")
        if (hasattr(self, "_bw_layout") and m
                and m != getattr(self, "_bw_mode", None)):
            self._rebuild_bw_presets(m)

    def _set_cursor(self, freq_hz, level):
        self.vline.setPos(freq_hz)
        self.vline_wf.setPos(freq_hz)
        if level is not None:
            self.hline.setPos(level)
            self.cursor_lbl.setText(
                f"cursor:  <b>{freq_hz/1e6:.5f} MHz</b>   "
                f"{level:+.1f} {self.unit}")
        else:
            self.cursor_lbl.setText(f"cursor:  <b>{freq_hz/1e6:.5f} MHz</b>")

    # -- per-frame update ----------------------------------------------------
    def _freq_axis(self):
        n = self.proc.n
        half = self.rate / 2.0
        return (self.center_hz + np.linspace(-half, half, n, endpoint=False)
                + self.ref_offset * 0)     # center in Hz

    def _tick(self):
        # follow live radio state / retunes from the control API
        self._update_center_from_ctrl()

        if self.rate != (self.iq.rate or self.rate):
            self.rate = self.iq.rate or self.rate

        self._update_info()
        self._sync_controls()

        if self.freeze:
            return
        iq = self.iq.latest(self.proc.n)
        if iq is None:
            return

        # Sideband orientation: the SunSDR2's RX IQ comes off the wire with the
        # spectrum mirrored relative to true RF (a USB signal, physically ABOVE
        # the dial, lands at NEGATIVE baseband). Conjugating before the FFT flips
        # it so positive offset = above the dial = right on screen, matching the
        # dial/filter overlay and rig convention. Display-only — the demod's own
        # decode path is unaffected. (self.spectrum_invert lets a differently-
        # wired setup turn it off.)
        if self.spectrum_invert:
            iq = np.conj(iq)

        spec_db, peak_db = self.proc.process(iq, kill_dc=self.kill_dc)
        disp = spec_db + self.ref_offset
        peak_disp = peak_db + self.ref_offset

        freqs = self._freq_axis()
        self.curve.setData(freqs, disp)
        if self.peak_hold:
            self.peak_curve.setData(freqs, peak_disp)

        # y-scale
        #
        # PERFORMANCE: each setYRange forces a full axis-tick + grid relayout +
        # repaint (~50-70 ms in software rendering) — by far the dominant cost.
        # With a STABLE range the whole pipeline (changing curve + waterfall)
        # runs at 190+ fps; calling setYRange EVERY frame (as continuous
        # autoscale did) is what pinned it to ~9 fps. So in autoscale we only
        # RECOMPUTE and re-apply the range on a timer (rescale_interval, default
        # 5 s) — the axis holds still and fast between rescales, and still tracks
        # the band. A big overshoot (signal clips past the top) forces an
        # immediate rescale so a sudden strong signal isn't clipped for 5 s.
        now = time.monotonic()
        if self.autoscale:
            cur_lo, cur_hi = self._applied_yrange
            due = (cur_lo is None
                   or (now - self._last_rescale) >= self.rescale_interval)
            top = float(np.max(disp))
            overshoot = cur_hi is not None and top > cur_hi + 3.0
            if due or overshoot:
                floor = float(np.percentile(disp, 20.0))
                lo = 5.0 * np.floor((floor - 8.0 - 3.0) / 5.0)
                hi = 5.0 * np.ceil((top + 12.0 + 3.0) / 5.0)
                self._last_rescale = now
                if (lo, hi) != self._applied_yrange:
                    self._applied_yrange = (lo, hi)
                    if self.pretty:
                        self.curve.setFillLevel(lo - 50)
                    self.p_spec.setYRange(lo, hi, padding=0)
        else:
            lo = self.ref_level - self.dyn_range
            hi = self.ref_level
            if (lo, hi) != self._applied_yrange:
                self._applied_yrange = (lo, hi)
                if self.pretty:
                    self.curve.setFillLevel(lo - 50)
                self.p_spec.setYRange(lo, hi, padding=0)
        ymin, ymax = self._applied_yrange

        # x-range respects the zoom factor (centered on the tuned freq). At
        # zoom=1 this is the full span [freqs[0], freqs[-1]]; when zoomed we show
        # a centered window. Only re-apply on change (keeps the perf win, and
        # lets a retune re-center the zoomed view).
        half = (freqs[-1] - freqs[0]) / 2.0 * self.zoom
        c = self._zoom_center_hz if self._zoom_center_hz is not None else self.center_hz
        want = (c - half, c + half)
        if want != self._applied_xrange:
            self._applied_xrange = want
            self.p_spec.setXRange(want[0], want[1], padding=0)

        self._update_waterfall(disp, freqs, ymin, ymax)

    def _update_waterfall(self, disp, freqs, ymin, ymax):
        n = len(disp)
        if self._wf is None or self._wf_n != n:
            self._wf = np.full((self.wf_history, n), ymin, dtype=np.float32)
            self._wf_n = n
        self._wf = np.roll(self._wf, 1, axis=0)
        self._wf[0, :] = disp
        self.img.setImage(self._wf, autoLevels=False, levels=(ymin, ymax))
        # place the image under the shared frequency x-axis
        f0, span = freqs[0], (freqs[-1] - freqs[0])
        self.img.setRect(QtCore.QRectF(f0, 0, span, self.wf_history))

    # -- info bar ------------------------------------------------------------
    def _update_center_from_ctrl(self):
        st = self.ctrl.state() if self.ctrl else {}
        f = st.get("freq")
        if f and f not in ("None", ""):
            try:
                self.center_hz = float(int(f))
            except ValueError:
                pass
        elif self.center_hz == 0.0 and self.iq.header_freq:
            self.center_hz = self.iq.header_freq

    def _update_info(self):
        rate = self.rate
        rbw = rate / self.proc.n
        parts = [f"<b>{self.center_hz/1e6:.5f} MHz</b>",
                 f"span {rate/1e3:.1f} kHz",
                 f"RBW {rbw:.1f} Hz",
                 f"FFT {self.proc.n}"]
        if self.ctrl and self.ctrl.available:
            st = self.ctrl.state()
            mode = st.get("mode", "—")
            if mode in ("None", ""):
                mode = "—"
            ptt = st.get("ptt", "off")
            sm = st.get("smeter")
            pw = st.get("power")
            streaming = st.get("streaming", "0")
            seg = [f"mode <b>{mode}</b>"]
            if sm and sm not in ("None", ""):
                seg.append(f"S <b>{float(sm):.0f}</b> dBFS")
            if ptt == "on":
                seg.append("<b style='color:#ff5555'>TX</b>")
            if pw and pw not in ("None", ""):
                seg.append(f"pwr {pw} W")
            seg.append(f"stream {'on' if streaming not in ('0','None') else 'off'}")
            parts += seg
            src = "control API: live"
        elif isinstance(self.iq, FileIQReader):
            src = "<b style='color:#ffcf6b'>DEMO</b> · file replay"
        else:
            src = "control API: n/a (IQ-only)"
        conn = ("<span style='color:#57d957'>IQ ●</span>" if self.iq.connected
                else "<span style='color:#ff6b6b'>IQ ○</span>")
        parts.append(f"<span style='color:#7f8c99'>{src}</span>")
        self.info.setText(conn + "  " + "   ".join(parts))

    # -- keyboard shortcuts --------------------------------------------------
    def keyPressEvent(self, e):
        k = e.key()
        txt = e.text()
        if txt in ("+", "="):          # '=' so you needn't hold shift for '+'
            self.zoom_by(0.5)
        elif txt == "-":
            self.zoom_by(2.0)
        elif txt == "0":
            self.zoom_full()
        elif k == KEY_A:
            self.cb_auto.toggle()
        elif k == KEY_R:
            self.rescale_now()
        elif k == KEY_P:
            self.cb_peak.toggle()
        elif k == KEY_SPACE:
            self.cb_freeze.toggle()
        elif k == KEY_C:
            i = (self.cmb_cmap.currentIndex() + 1) % self.cmb_cmap.count()
            self.cmb_cmap.setCurrentIndex(i)
        elif k in (KEY_Q, KEY_ESCAPE):
            self.close()
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        self.timer.stop()
        self.iq.stop()
        if self.ctrl:
            self.ctrl.stop()
        super().closeEvent(e)


def main():
    p = argparse.ArgumentParser(
        description="solsdr panadapter + control panel — live spectrum, "
                    "waterfall, click-to-tune, mode & frequency control")
    p.add_argument("--host", default="127.0.0.1",
                   help="solsdr host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=5555,
                   help="IQ server port (default 5555)")
    p.add_argument("--control-port", type=int, default=5556,
                   help="control API port for live radio state (default 5556)")
    p.add_argument("--no-control", action="store_true",
                   help="don't poll the control API (spectrum/waterfall only)")
    p.add_argument("--fft", type=int, default=4096,
                   help="initial FFT size (default 4096)")
    p.add_argument("--ref-offset", type=float, default=0.0,
                   help="dB added to the dBFS scale to read approximate dBm "
                        "(calibrate for your setup; default 0 => axis is dBFS)")
    p.add_argument("--history", type=int, default=400,
                   help="waterfall history depth in rows (default 400)")
    p.add_argument("--pretty", action="store_true",
                   help="antialiased, filled, thicker spectrum trace — nicer on "
                        "a GPU/fast box, but ~50x more CPU per frame in software "
                        "rendering (default is a fast thin non-AA trace).")
    p.add_argument("--rescale", type=float, default=5.0, metavar="SEC",
                   help="auto-scale recomputes the level range every SEC seconds "
                        "(default 5). Re-ranging repaints the axis and is the "
                        "main CPU cost, so a longer interval = higher frame rate; "
                        "press R (or the Rescale button) to snap immediately.")
    p.add_argument("--file", default=None, metavar="PATH",
                   help="replay a recorded wire-format IQ capture instead of "
                        "connecting to a live radio (demo/offline). Made by "
                        "tools/capture_iq_stream.py. Loops at EOF.")
    p.add_argument("--no-loop", dest="loop", action="store_false",
                   help="with --file, stop at EOF instead of looping")
    p.add_argument("--file-rate", type=float, default=None,
                   help="sample rate for a headerless raw complex64 --file")
    p.add_argument("--file-freq", type=float, default=None,
                   help="center freq (Hz) for a headerless raw complex64 --file")
    args = p.parse_args()

    if args.file:
        iq = FileIQReader(args.file, loop=args.loop,
                          rate_override=args.file_rate,
                          freq_override=args.file_freq)
        ctrl = None                    # no live radio in file/demo mode
    else:
        iq = IQReader(args.host, args.port)
        ctrl = (None if args.no_control
                else ControlPoller(args.host, args.control_port))
    iq.start()
    if ctrl:
        ctrl.start()

    if ctrl is None and not args.file:
        print("no control API (--no-control): running as a display only — "
              "tuning and the mode/frequency controls are disabled.",
              file=sys.stderr)
    app = QtWidgets.QApplication(sys.argv)
    win = Panadapter(iq, ctrl, fft_size=args.fft, ref_offset=args.ref_offset,
                     wf_history=args.history, rescale_interval=args.rescale,
                     pretty=args.pretty)
    win.show()

    # let Ctrl-C in the terminal close the window
    timer = QtCore.QTimer()
    timer.start(300)
    timer.timeout.connect(lambda: None)
    app.exec_() if hasattr(app, "exec_") else app.exec()

    iq.stop()
    if ctrl:
        ctrl.stop()


if __name__ == "__main__":
    main()
