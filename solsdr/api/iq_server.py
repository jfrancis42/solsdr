"""
Network IQ streaming server.

Streams raw complex64 IQ from the radio to any number of TCP clients. This is
the core of the "GNU Radio is a client of the SDR" architecture: the SDR does
protocol + tuning; clients (GNU Radio flowgraphs, recorders, custom DSP) pull
raw IQ over the network and do whatever they want with it.

Wire format on the TCP stream: little-endian interleaved float32 I,Q pairs
(i.e. numpy complex64.tobytes()) — the de-facto standard GNU Radio and most
SDR tooling expect for a "complex float" TCP source. A tiny text header line is
sent once on connect so clients can self-configure:

    SOLSDR IQ rate=39062.5 fmt=complex64 freq=14074000\n

Clients that don't care can ignore the header (it ends at the first newline)
and just read complex64 samples after it.

Feed it by registering iq_server.publish as the Radio stream callback.
"""

import socket
import struct
import threading

import numpy as np


class IQStreamServer:
    def __init__(self, host='0.0.0.0', port=5555, verbose=True):
        self.host = host
        self.port = port
        self.verbose = verbose
        self._sock = None
        self._running = False
        self._thread = None
        self._clients = []
        self._clients_lock = threading.Lock()
        # metadata sent in the connect header
        self.rate = 39062.5
        self.freq = 0

    def _log(self, *a):
        if self.verbose:
            print('[iq-server]', *a)

    def start(self, rate=None, freq=None):
        if rate is not None:
            self.rate = rate
        if freq is not None:
            self.freq = freq
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        self._log(f'listening on {self.host}:{self.port} '
                  f'(complex64 @ {self.rate:.1f} Hz)')

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)
        with self._clients_lock:
            for c in self._clients:
                try:
                    c.close()
                except OSError:
                    pass
            self._clients = []
        if self._sock:
            self._sock.close()

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            header = (f'SOLSDR IQ rate={self.rate} fmt=complex64 '
                      f'freq={self.freq}\n').encode()
            try:
                conn.sendall(header)
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                conn.close()
                continue
            with self._clients_lock:
                self._clients.append(conn)
            self._log(f'client connected {addr} ({len(self._clients)} total)')

    def publish(self, iq: np.ndarray):
        """Radio stream callback: broadcast one IQ block to all clients."""
        if not self._clients:
            return
        data = np.ascontiguousarray(iq, dtype=np.complex64).tobytes()
        dead = []
        with self._clients_lock:
            for c in self._clients:
                try:
                    c.sendall(data)
                except OSError:
                    dead.append(c)
            for c in dead:
                self._clients.remove(c)
                try:
                    c.close()
                except OSError:
                    pass
        if dead:
            self._log(f'{len(dead)} client(s) disconnected')

    @property
    def client_count(self):
        with self._clients_lock:
            return len(self._clients)
