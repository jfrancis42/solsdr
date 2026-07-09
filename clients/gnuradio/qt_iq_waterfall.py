#!/usr/bin/env python3
"""
GNU Radio Qt waterfall + FFT viewer for the solsdr IQ stream (SunSDR2 PRO).

Connects to solsdr's raw-IQ TCP server (`solsdr --iq-server` (or `python3 -m solsdr`), or the
IQStreamServer on port 5555), displays a live FFT trace and a waterfall. This is
the "is it really working?" demo: run it, see your SunSDR2 IQ in GNU Radio.

The solsdr IQ stream is little-endian interleaved float32 I,Q (numpy complex64)
over TCP, preceded by a one-line text header sent once on connect:

    SOLSDR IQ rate=39062.5 fmt=complex64 freq=14074000\n

This viewer connects, reads and parses that header (so it auto-configures the
sample rate and center-frequency labels — no need to pass --rate), then streams
the complex64 samples that follow into GNU Radio via a small custom source block.

Usage:
    # on the machine running solsdr:
    python3 -m solsdr 14074 --iq-server
    # then, anywhere that can reach it (needs X/Wayland; use ssh -X for remote):
    python3 clients/gnuradio/qt_iq_waterfall.py --host 127.0.0.1 --port 5555

Requires: gnuradio (with qtgui), PyQt5. Install per your distro's GNU Radio
packages; this is a client-side dependency, not required by solsdr itself.
"""
import argparse
import signal
import socket
import sys
import threading

import numpy as np
from PyQt5 import Qt
from gnuradio import gr, qtgui, fft as gr_fft
try:
    import sip
except ImportError:            # newer PyQt5 ships sip under the package
    from PyQt5 import sip


def read_header(sock):
    """Read the one-line 'SOLSDR IQ ...' text header, return (rate, freq, dict).

    Reads one byte at a time up to the first newline so we don't consume any of
    the binary complex64 samples that follow it."""
    buf = bytearray()
    while b'\n' not in buf and len(buf) < 512:
        b = sock.recv(1)
        if not b:
            break
        buf += b
    line = buf.split(b'\n', 1)[0].decode('ascii', 'replace').strip()
    fields = {}
    for tok in line.split():
        if '=' in tok:
            k, v = tok.split('=', 1)
            fields[k] = v
    rate = float(fields.get('rate', 39062.5))
    freq = float(fields.get('freq', 0.0))
    return rate, freq, line


class _SolsdrTCPSource(gr.sync_block):
    """GNU Radio source that pulls complex64 IQ from an already-connected
    solsdr IQ socket (header already stripped). A background thread does the
    blocking socket reads into a buffer; work() drains it."""

    def __init__(self, sock):
        gr.sync_block.__init__(self, name='solsdr_tcp_source',
                               in_sig=None, out_sig=[np.complex64])
        self._sock = sock
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._running = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self):
        while self._running:
            try:
                data = self._sock.recv(65536)
            except OSError:
                break
            if not data:
                break
            with self._lock:
                self._buf += data

    def work(self, input_items, output_items):
        out = output_items[0]
        need_bytes = len(out) * 8            # complex64 = 8 bytes/sample
        with self._lock:
            have = len(self._buf) - (len(self._buf) % 8)
            n_bytes = min(need_bytes, have)
            if n_bytes == 0:
                return 0
            chunk = bytes(self._buf[:n_bytes])
            del self._buf[:n_bytes]
        n = n_bytes // 8
        out[:n] = np.frombuffer(chunk, dtype=np.complex64, count=n)
        return n

    def stop(self):
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass
        return True


class IQViewer(gr.top_block, Qt.QWidget):
    def __init__(self, sock, sample_rate, center_freq, fft_size=1024,
                 y_min=-130.0, y_max=-50.0, wf_min=None, wf_max=None,
                 gain_db=0.0, autoscale=False, show_labels=True,
                 update_time=0.03):
        gr.top_block.__init__(self, "solsdr IQ Viewer")
        Qt.QWidget.__init__(self)
        self.setWindowTitle(f"solsdr IQ Viewer @ {center_freq/1e6:.4f} MHz "
                            f"({sample_rate/1000:.1f} kHz BW)")
        # Waterfall intensity defaults track the FFT y-axis if not given.
        if wf_min is None:
            wf_min = y_min
        if wf_max is None:
            wf_max = y_max

        layout = Qt.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- Source: solsdr TCP complex64 ----
        self.src = _SolsdrTCPSource(sock)

        # Optional linear gain to lift the low-amplitude solsdr IQ into the
        # display range (purely cosmetic — shifts the dB scale, doesn't change
        # the radio). gain_db is applied as a complex multiply before the sinks.
        self._gain = None
        if abs(gain_db) > 1e-6:
            from gnuradio import blocks
            lin = 10.0 ** (gain_db / 20.0)
            self._gain = blocks.multiply_const_cc(lin)

        # ---- Combined FFT + waterfall in ONE sink ----
        # qtgui.sink_c draws the PSD (FFT) and spectrogram (waterfall) stacked
        # in a single widget that SHARES one frequency x-axis, so a peak in the
        # FFT sits directly above its hotspot in the waterfall — aligned by
        # construction, with the axis labels intact. Two separate sinks can't
        # guarantee that (their canvases differ in width). We enable freq +
        # waterfall and disable the time-domain and constellation plots.
        #   sink_c(fftsize, wintype, fc, bw, name,
        #          plotfreq, plotwaterfall, plottime, plotconst, parent)
        self.sink = qtgui.sink_c(
            fft_size, gr_fft.window.WIN_BLACKMAN_hARRIS,
            center_freq, sample_rate, "solsdr",
            True,    # plotfreq (FFT)
            True,    # plotwaterfall
            False,   # plottime
            False,   # plotconst
            None,
        )
        # qtgui.sink_c is a self-contained combined scope: it exposes very few
        # Python setters (basically set_update_time). Grid/labels/y-axis/
        # intensity are adjusted via the widget's own right-click menu at
        # runtime. Call every optional setter defensively so we work across GNU
        # Radio versions regardless of which are present.
        def _try(name, *a):
            fn = getattr(self.sink, name, None)
            if fn:
                try:
                    fn(*a)
                except Exception:
                    pass
        _try('set_update_time', update_time)
        _try('enable_grid', True)
        _try('enable_axis_labels', show_labels)
        _try('set_y_axis', y_min, y_max)
        _try('set_intensity_range', wf_min, wf_max)
        # Label the x-axis with the ABSOLUTE RF frequency (e.g. 7.125 MHz)
        # rather than baseband offset (±kHz around 0). set_frequency_range sets
        # the center/span; enable_rf_freq flips the axis from baseband to RF.
        _try('set_frequency_range', center_freq, sample_rate)
        _try('enable_rf_freq', True)
        layout.addWidget(sip.wrapinstance(self.sink.qwidget(), Qt.QWidget))

        # ---- Wire it up (optionally through the gain block) ----
        head = self.src
        if self._gain is not None:
            self.connect(self.src, self._gain)
            head = self._gain
        self.connect(head, self.sink)


def main():
    p = argparse.ArgumentParser(description="GNU Radio waterfall for the solsdr "
                                            "SunSDR2 PRO IQ stream")
    p.add_argument('--host', default='127.0.0.1',
                   help="solsdr IQ server host (default 127.0.0.1)")
    p.add_argument('--port', type=int, default=5555,
                   help="solsdr IQ server port (default 5555)")
    p.add_argument('--rate', type=float, default=None,
                   help="override IQ sample rate (default: read from stream header)")
    p.add_argument('--freq', type=float, default=None,
                   help="override center frequency for labels "
                        "(default: read from stream header)")
    p.add_argument('--fft', type=int, default=1024, help="FFT size (default 1024)")
    # Display scaling. solsdr IQ is low-amplitude (peaks ~-90 dBFS, floor
    # ~-120), so the fixed defaults are tuned for that, not a full-scale radio.
    p.add_argument('--ymin', type=float, default=-130.0,
                   help="FFT y-axis min in dB (default -130)")
    p.add_argument('--ymax', type=float, default=-50.0,
                   help="FFT y-axis max in dB (default -50)")
    p.add_argument('--wf-min', type=float, default=None,
                   help="waterfall intensity min dB (default: --ymin)")
    p.add_argument('--wf-max', type=float, default=None,
                   help="waterfall intensity max dB (default: --ymax)")
    p.add_argument('--gain', type=float, default=70.0,
                   help="cosmetic display gain in dB applied to the IQ before "
                        "the sink, to lift solsdr's low-amplitude IQ (~-90 dBFS) "
                        "into the visible range. Does NOT change the radio and "
                        "makes the dB scale relative, not absolute. Default 70; "
                        "use 0 for true levels, or tune to taste.")
    p.add_argument('--autoscale', action='store_true',
                   help="let the FFT trace autoscale its y-axis instead of using "
                        "--ymin/--ymax (quickest fix if the trace is off-screen)")
    p.add_argument('--no-labels', dest='labels', action='store_false',
                   help="hide the axis labels for a little more plot width "
                        "(labels are on by default and stay aligned)")
    p.add_argument('--update', type=float, default=0.03,
                   help="display update interval in seconds; smaller = faster "
                        "refresh (default 0.03 = ~30 fps). Try 0.02 for snappier, "
                        "0.05 for lighter CPU.")
    args = p.parse_args()

    # Connect and read the solsdr header so we can auto-configure.
    try:
        sock = socket.create_connection((args.host, args.port), timeout=5)
    except OSError as e:
        print(f"could not connect to solsdr IQ server at "
              f"{args.host}:{args.port}: {e}", file=sys.stderr)
        print("start it with:  python3 -m solsdr <kHz> --iq-server",
              file=sys.stderr)
        sys.exit(1)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    hdr_rate, hdr_freq, hdr_line = read_header(sock)
    print(f"connected: {hdr_line}")
    rate = args.rate if args.rate is not None else hdr_rate
    freq = args.freq if args.freq is not None else hdr_freq

    qapp = Qt.QApplication(sys.argv)
    fg = IQViewer(sock, rate, freq, fft_size=args.fft,
                  y_min=args.ymin, y_max=args.ymax,
                  wf_min=args.wf_min, wf_max=args.wf_max,
                  gain_db=args.gain, autoscale=args.autoscale,
                  show_labels=args.labels, update_time=args.update)
    fg.start()
    fg.show()

    # Clean shutdown: tear the flowgraph down before Qt destroys the widgets,
    # or GR's background threads race Python finalisation and segfault.
    def stop_flowgraph():
        fg.stop()
        fg.wait()

    qapp.aboutToQuit.connect(stop_flowgraph)
    signal.signal(signal.SIGINT, lambda *_: Qt.QApplication.quit())

    # Let Python signal handlers run while Qt is in its native event loop.
    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    qapp.exec_()
    del fg


if __name__ == '__main__':
    main()
