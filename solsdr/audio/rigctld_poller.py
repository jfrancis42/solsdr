"""
Real-Hamlib CAT for the JS8Call/WSJT-X audio bridge.

JS8Call talks to a genuine `rigctld -m 1` (Hamlib dummy backend), so all the
CAT/PTT/split protocol handling is Hamlib's own battle-tested code — not a
hand-rolled reimplementation. This module:

  1. launches `rigctld -m 1` on the CAT port, and
  2. connects to it as a SECOND client, polling freq / mode / PTT, and mirrors
     any change to the SunSDR2 (freq/mode via the radio, PTT via the bridge).

This mirrors the proven architecture of hamlib-audio-sidecar (which connects to
a real rigctld rather than serving the protocol itself). The distributed Hamlib
has no SunSDR2 backend, so the dummy backend is used purely as a faithful state
store that JS8Call drives and we read back.
"""
import shutil
import socket
import subprocess
import threading
import time


class RigctldPoller:
    def __init__(self, radio, ptt_callback, host='127.0.0.1', port=4532,
                 model='1', poll_hz=20, verbose=True):
        """
        radio: high-level Radio (set_frequency / set_mode / current_*).
        ptt_callback: called with True/False on a PTT edge (the bridge keys TX).
        model: Hamlib rig model for rigctld (-m). '1' = dummy state store.
        """
        self.radio = radio
        self.ptt_callback = ptt_callback
        self.host = host
        self.port = int(port)
        self.model = str(model)
        self.interval = 1.0 / poll_hz
        self.verbose = verbose
        self._rigctld = None
        self._sock = None
        self._thread = None
        self._running = False
        # last-seen state (so we only act on changes)
        self._last_freq = None
        self._last_mode = None
        self._last_ptt = None

    def _log(self, msg):
        if self.verbose:
            print(f'[rigctld] {msg}')

    # -- lifecycle --------------------------------------------------------
    def start(self):
        if shutil.which('rigctld') is None:
            raise RuntimeError('rigctld not found — install Hamlib (hamlib / '
                               'libhamlib-utils)')
        # Launch real rigctld with the dummy backend as the CAT endpoint.
        self._rigctld = subprocess.Popen(
            ['rigctld', '-m', self.model, '-T', self.host, '-t', str(self.port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Wait for it to accept connections.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                s = socket.create_connection((self.host, self.port), timeout=1)
                s.close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError('rigctld did not come up on '
                               f'{self.host}:{self.port}')
        # Seed rigctld with the radio's current freq/mode so JS8Call reads sane
        # values immediately.
        f = self.radio.current_freq
        m = self.radio.current_mode
        if f:
            self._cmd(f'F {int(f)}\n')
        if m:
            self._cmd(f'M {self._mode_to_hamlib(m)} 3000\n')
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True,
                                        name='rigctld-poller')
        self._thread.start()
        self._log(f'real rigctld -m {self.model} on {self.host}:{self.port}; '
                  f'polling freq/mode/PTT')

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._rigctld is not None:
            try:
                self._rigctld.terminate()
                self._rigctld.wait(timeout=2)
            except Exception:
                self._rigctld.kill()
            self._rigctld = None

    # -- rigctld client ---------------------------------------------------
    def _connect(self):
        self._sock = socket.create_connection((self.host, self.port), timeout=2)

    def _cmd(self, s):
        """Send a command on a persistent client socket, return the reply."""
        for attempt in (1, 2):
            try:
                if self._sock is None:
                    self._connect()
                self._sock.sendall(s.encode())
                self._sock.settimeout(1.0)
                return self._sock.recv(4096).decode(errors='replace')
            except OSError:
                if self._sock is not None:
                    try:
                        self._sock.close()
                    except OSError:
                        pass
                self._sock = None
                if attempt == 2:
                    return ''
        return ''

    def _poll_loop(self):
        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                self._log(f'poll error: {e}')
            time.sleep(self.interval)

    def _poll_once(self):
        # PTT is the time-critical one — poll it every cycle.
        ptt_raw = self._cmd('t\n').strip()
        if ptt_raw in ('0', '1'):
            ptt = (ptt_raw == '1')
            if ptt != self._last_ptt:
                first = self._last_ptt is None
                self._last_ptt = ptt
                # Don't fire on the initial baseline read (None -> 0); only on
                # genuine edges. A spurious startup "unkey" is harmless but
                # could trigger an unwanted exit_tx.
                if not (first and not ptt):
                    try:
                        self.ptt_callback(ptt)
                    except Exception as e:
                        self._log(f'ptt callback error: {e}')

        # Freq/mode change less often; check every cycle but only act on change.
        f_raw = self._cmd('f\n').strip()
        if f_raw.isdigit():
            freq = int(f_raw)
            if freq != self._last_freq:
                self._last_freq = freq
                if self.radio.current_freq != freq:
                    self.radio.set_frequency(freq)

        m_raw = self._cmd('m\n').strip().split('\n')
        if m_raw and m_raw[0]:
            mode = self._hamlib_to_mode(m_raw[0])
            if mode != self._last_mode:
                self._last_mode = mode
                if self.radio.current_mode != mode:
                    self.radio.set_mode(mode)

    # -- mode maps --------------------------------------------------------
    @staticmethod
    def _hamlib_to_mode(h):
        return {'USB': 'USB', 'LSB': 'LSB', 'CW': 'CW', 'CWR': 'CW',
                'AM': 'AM', 'FM': 'FM', 'PKTUSB': 'USB', 'PKTLSB': 'LSB',
                'RTTY': 'LSB', 'DATA': 'USB'}.get(h.upper(), 'USB')

    @staticmethod
    def _mode_to_hamlib(m):
        # JS8/FT8 are USB data; keep it simple and valid for the dummy.
        return {'USB': 'PKTUSB', 'LSB': 'PKTLSB', 'CW': 'CW',
                'AM': 'AM', 'FM': 'FM'}.get(str(m).upper(), 'USB')
