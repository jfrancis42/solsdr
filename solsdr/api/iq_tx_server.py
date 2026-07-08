"""
Raw-IQ TX server — the transmit counterpart to iq_server.py.

Accepts a TCP client that sends raw **complex64** baseband IQ at the radio wire
rate (e.g. 39062.5 Hz for the PRO), and streams it to the radio as 0xFD TX
frames through a safety-interlocked TXSession. This is the raw-IQ path for GNU
Radio and custom waveform generators: no modulator, no resampling — the samples
you send are what the radio transmits (subject to gain + clip).

Symmetry with the RX side:
    RX:  radio -> IQStreamServer  -> TCP client (GNU Radio TCP Source)
    TX:  TCP client (GNU Radio TCP Sink) -> IQTXServer -> radio

Wire format the client must produce (mirror of the RX header):
    SOLSDR IQTX rate=39062.5 fmt=complex64\n<interleaved little-endian float32 I,Q ...>
The client MAY skip the header entirely and just send complex64 samples — the
server sends the header for symmetry/inspection but does not require one back.

SAFETY: keying is gated exactly like every other TX path. The server constructs
its TXSession with the same max_drive / max_power_watts limits, arms it only if
armed=True was passed, and keys for the lifetime of a client connection:

    connect  -> key TX (enter_tx iq_input=True, sourced from the socket)
    stream   -> paced 0xFD frames
    disconnect / idle timeout / stop() -> unkey (exit_tx)

Only ONE client may transmit at a time (the radio has one TX). A second
connection is refused while one is keyed.

Sample-rate contract: the client MUST send IQ already at the radio wire rate.
There is no resampler here (unlike the audio path), so a rate mismatch
transmits at the wrong speed. The announced rate in the header is the required
rate; read it and configure your flowgraph to match.
"""
import socket
import threading
import time

import numpy as np


class IQTXServer:
    def __init__(self, radio, host='127.0.0.1', port=5558, *,
                 mode='USB', armed=False, max_drive=255, max_power_watts=None,
                 watts=None, iq_gain=1.0, idle_timeout=2.0, verbose=True):
        """
        radio: an opened Radio (provides .ctrl, .profile, .wire_rate, rx_sock).
        armed: if False (default), the full chain runs to the radio's TX port
            but PTT is never asserted — no RF, for safe wiring tests. Set True
            (deliberately) to actually key.
        max_drive / max_power_watts / watts: passed to the TXSession — the same
            amp-protection interlocks as every other TX path.
        iq_gain: linear gain applied to incoming IQ before clipping (the raw-IQ
            path is not auto-leveled; samples are expected in [-1, 1]).
        idle_timeout: if no IQ arrives for this long while keyed, unkey (guards
            against a client that connects, keys, then stalls).
        """
        self.radio = radio
        self.host = host
        self.port = int(port)
        self.mode = mode.upper()
        self.armed = bool(armed)
        self.max_drive = max_drive
        self.max_power_watts = max_power_watts
        self.watts = watts
        self.iq_gain = float(iq_gain)
        self.idle_timeout = float(idle_timeout)
        self.verbose = verbose
        self.wire_rate = radio.wire_rate

        self._sock = None
        self._thread = None
        self._running = False
        self._tx = None
        self._tx_lock = threading.Lock()
        # Set True the moment we begin keying, BEFORE enter_tx() consumes the IQ
        # iterator (self._tx isn't assigned until enter_tx returns, so the
        # iterator can't gate on it or it would end during prebuffer).
        self._feeding = False
        self._reader_stop = threading.Event()
        self._client = None
        # bytes-per-complex64 sample (I + Q, float32 each)
        self._SAMP = 8
        # feed queue between the socket reader and the TXSession iterator
        self._q = []
        self._q_lock = threading.Lock()
        self._last_rx = 0.0
        self.samples_received = 0

    def _log(self, *a):
        if self.verbose:
            from ..log import log_line
            log_line('iq-tx', ' '.join(str(x) for x in a))

    # -- lifecycle --------------------------------------------------------
    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(1)
        self._sock.settimeout(0.5)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        self._log(f'listening on {self.host}:{self.port} '
                  f'(complex64 @ {self.wire_rate:.1f} Hz, '
                  f'{"ARMED" if self.armed else "no-RF"})')

    def stop(self):
        self._running = False
        with self._tx_lock:
            self._unkey()
        if self._client is not None:
            try:
                self._client.close()
            except OSError:
                pass
            self._client = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    # -- accept / receive -------------------------------------------------
    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            # One transmitter at a time.
            with self._tx_lock:
                busy = self._tx is not None
            if busy:
                self._log(f'refusing {addr}: TX already in use')
                try:
                    conn.sendall(b'ERR TX busy\n')
                    conn.close()
                except OSError:
                    pass
                continue
            self._client = conn
            t = threading.Thread(target=self._client_session,
                                 args=(conn, addr), daemon=True)
            t.start()

    def _client_session(self, conn, addr):
        self._log(f'client connected {addr} — keying TX')
        conn.settimeout(0.5)
        try:
            conn.sendall((f'SOLSDR IQTX rate={self.wire_rate} '
                          f'fmt=complex64\n').encode())
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            conn.close()
            self._client = None
            return

        with self._q_lock:
            self._q = []
        self._last_rx = time.time()
        self.samples_received = 0

        # Start the socket reader FIRST (on its own thread) so it fills the IQ
        # queue while _key()->enter_tx() blocks in its prebuffer wait. If we
        # keyed first, enter_tx would block waiting for IQ that only this loop
        # supplies — a deadlock until the prebuffer timeout.
        self._reader_stop = threading.Event()
        reader = threading.Thread(target=self._reader_loop, args=(conn,),
                                  daemon=True)
        reader.start()

        keyed = self._key()
        if not keyed:
            self._reader_stop.set()
        try:
            # Hold the session open until the reader ends (disconnect / idle /
            # stop). The reader owns the socket lifetime.
            while self._running and keyed and not self._reader_stop.is_set():
                if self._last_rx and (time.time() - self._last_rx) > self.idle_timeout:
                    self._log(f'idle > {self.idle_timeout}s — unkeying')
                    break
                time.sleep(0.1)
        finally:
            self._reader_stop.set()
            reader.join(timeout=1.0)
            with self._tx_lock:
                self._unkey()
            try:
                conn.close()
            except OSError:
                pass
            self._client = None
            self._log(f'client {addr} disconnected — TX unkeyed '
                      f'({self.samples_received} samples)')

    def _reader_loop(self, conn):
        """Read complex64 samples off the socket into the feed queue. Consumes
        only whole 8-byte samples; carries a partial-sample remainder across
        recvs. Ends on EOF / error / stop, signalling via _reader_stop."""
        buf = b''
        try:
            while self._running and not self._reader_stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                n = len(buf) - (len(buf) % self._SAMP)
                if n:
                    iq = np.frombuffer(buf[:n], dtype=np.complex64)
                    buf = buf[n:]
                    with self._q_lock:
                        self._q.append(iq)
                    self.samples_received += len(iq)
                    self._last_rx = time.time()
        finally:
            self._reader_stop.set()

    def _iq_iter(self):
        """Yield IQ blocks pulled from the socket-reader queue; emit short
        silence on underrun so the 5.12 ms pacer never starves. Ends when the
        session stops keying."""
        block_silence = np.zeros(256, dtype=np.complex64)
        while True:
            if not self._feeding or not self._running:
                return
            with self._q_lock:
                if self._q:
                    out = np.concatenate(self._q)
                    self._q = []
                else:
                    out = None
            if out is not None and len(out):
                yield out
            else:
                yield block_silence
                time.sleep(0.003)

    # -- keying -----------------------------------------------------------
    def _key(self) -> bool:
        from ..tx_session import TXSession
        mode = self.radio.current_mode or self.mode
        with self._tx_lock:
            if self._tx is not None:
                return False
            tx = TXSession(self.radio, mode=mode, realtime=True,
                           max_drive=self.max_drive,
                           max_power_watts=self.max_power_watts,
                           verbose=self.verbose)
            tx.tx_iq_gain = self.iq_gain
            if self.armed:
                tx.arm(confirm=True)
            watts = self.watts if self.watts is not None else self.max_power_watts
            self._last_rx = time.time()
            self._feeding = True
            try:
                tx.enter_tx(self._iq_iter(), watts=watts, pa=False,
                            prebuffer_s=0.05, iq_input=True)
            except Exception as e:
                self._feeding = False
                self._log(f'enter_tx failed: {e}')
                return False
            self._tx = tx
            self._log(f'TX keyed (raw IQ, {mode}, '
                      f'{("%.1f W" % watts) if watts is not None else "full drive"}'
                      f'{"" if self.armed else ", NO RF"})')
            return True

    def _unkey(self):
        self._feeding = False
        if self._tx is not None:
            try:
                self._tx.exit_tx()
            except Exception as e:
                self._log(f'exit_tx error: {e}')
            self._tx = None
