#!/usr/bin/env python3
"""
solsdr panadapter — a live spectrum + waterfall display for the SunSDR2 PRO.

A standalone, visually-nice panadapter that reads solsdr's raw-IQ TCP stream
(`python3 -m solsdr`, IQ server on port 5555, on by default) and, optionally, its
text control API (port 5556) for live radio state. Pure Python: PyQt5 +
pyqtgraph + numpy. No GNU Radio, no ExpertSDR3.

DISPLAY ONLY — this tool never tunes, keys, or changes the radio. It shows you
what's going on. (Control may come later.)

Features
--------
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
    # live: on the machine running solsdr (RX IQ is on by default) —
    python3 -m solsdr 14074 --control-api

    # then, anywhere that can reach it (needs a display; ssh -X for remote):
    python3 clients/panadapter.py --host 127.0.0.1

    # demo / offline: replay a recorded capture, no radio needed —
    python3 clients/panadapter.py --file clients/examples/solsdr_20m_demo30.iq

Options: --host, --port (IQ, 5555), --control-port (5556), --file (replay a
wire-format capture; loops at EOF), --fft, --ref-offset (dBFS→dBm), --rescale
(auto-scale cadence, s), --pretty (antialiased filled trace), --no-control. Run
with --help for all. Keys: +/- zoom (0 = full) · A auto-scale · R rescale now ·
P peak-hold · C colormap · space freeze · Q quit. (Zoom +/- buttons are in the
toolbar; mouse-drag/scroll on the plot also zooms/pans.)

Notes
-----
  * solsdr RX has no absolute power calibration, so the axis is **dBFS** by
    default (0 dBFS = a full-scale complex tone). If you have measured the
    offset for your setup, pass --ref-offset <dB> to relabel the axis as dBm.
  * solsdr's control API reports freq/mode/PTT/power/S-meter but NOT AGC / NR /
    filter / preamp (those are set-only), so the info bar shows what's queryable.
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
        leftover = b""
        while self._running:
            try:
                data = sock.recv(65536)
            except OSError:
                break
            if not data:
                break
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
# Control API poller (background thread) — optional, display-only
# ─────────────────────────────────────────────────────────────────────────────
class ControlPoller(threading.Thread):
    """Polls solsdr's text control API `status` for live radio state. Purely a
    reader — never sends a control command. Reconnects on failure."""

    def __init__(self, host, port, interval=0.5):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.interval = interval
        self._running = True
        self._lock = threading.Lock()
        self._state = {}
        self.available = False

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
CMAP_NAMES = ["inferno", "viridis", "magma", "plasma", "turbo", "CET-L9",
              "cividis"]


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
# Main window
# ─────────────────────────────────────────────────────────────────────────────
class Panadapter(QtWidgets.QMainWindow):
    def __init__(self, iq: IQReader, ctrl: ControlPoller, *,
                 fft_size=2048, ref_offset=0.0, wf_history=400,
                 update_ms=33, rescale_interval=5.0, pretty=False):
        super().__init__()
        self.iq = iq
        self.ctrl = ctrl
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
        self.cmap_name = "inferno"

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

        vbox.addLayout(self._build_controls())

        # info bar
        self.info = QtWidgets.QLabel("—")
        self.info.setTextFormat(RICHTEXT)
        self.info.setStyleSheet("QLabel{color:#dfe6ee;font-size:12px;"
                                "padding:2px 4px;}")
        vbox.addWidget(self.info)

        # spectrum + waterfall in a draggable splitter
        splitter = QtWidgets.QSplitter(VERTICAL)
        vbox.addWidget(splitter, 1)

        # spectrum plot
        self.gl_spec = pg.GraphicsLayoutWidget()
        self.p_spec = self.gl_spec.addPlot()
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

        # waterfall plot (shares the spectrum's x-axis)
        self.gl_wf = pg.GraphicsLayoutWidget()
        self.p_wf = self.gl_wf.addPlot()
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
        self._mouse_proxy = pg.SignalProxy(self.p_spec.scene().sigMouseMoved,
                                           rateLimit=60, slot=self._on_mouse)

        # cursor readout
        self.cursor_lbl = QtWidgets.QLabel("cursor: —")
        self.cursor_lbl.setStyleSheet("QLabel{color:#9fb3c8;font-size:12px;}")
        vbox.addWidget(self.cursor_lbl)

    def _build_controls(self):
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)

        self.cb_auto = QtWidgets.QCheckBox("Auto-scale")
        self.cb_auto.setChecked(True)
        self.cb_auto.toggled.connect(self._on_auto)
        row.addWidget(self.cb_auto)

        self.btn_rescale = QtWidgets.QPushButton("Rescale")
        self.btn_rescale.setToolTip("Recompute the auto-scale range now "
                                    "(auto-rescale runs every few seconds)")
        self.btn_rescale.clicked.connect(self.rescale_now)
        row.addWidget(self.btn_rescale)

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

        row.addWidget(QtWidgets.QLabel("FFT"))
        self.cmb_fft = QtWidgets.QComboBox()
        for n in (512, 1024, 2048, 4096, 8192):
            self.cmb_fft.addItem(str(n), n)
        self.cmb_fft.setCurrentText("2048")
        self.cmb_fft.currentIndexChanged.connect(self._on_fft)
        row.addWidget(self.cmb_fft)

        row.addWidget(QtWidgets.QLabel("Avg"))
        self.sl_avg = QtWidgets.QSlider(HORIZONTAL)
        self.sl_avg.setRange(1, 100)
        self.sl_avg.setValue(35)
        self.sl_avg.setFixedWidth(90)
        self.sl_avg.valueChanged.connect(self._on_avg)
        row.addWidget(self.sl_avg)

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

        if self.freeze:
            return
        iq = self.iq.latest(self.proc.n)
        if iq is None:
            return

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
        description="solsdr panadapter — live spectrum + waterfall (display only)")
    p.add_argument("--host", default="127.0.0.1",
                   help="solsdr host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=5555,
                   help="IQ server port (default 5555)")
    p.add_argument("--control-port", type=int, default=5556,
                   help="control API port for live radio state (default 5556)")
    p.add_argument("--no-control", action="store_true",
                   help="don't poll the control API (spectrum/waterfall only)")
    p.add_argument("--fft", type=int, default=2048,
                   help="initial FFT size (default 2048)")
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
