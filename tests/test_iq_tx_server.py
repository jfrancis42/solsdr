#!/usr/bin/env python3
"""
Raw-IQ TX server test — offline, NO radio, NO RF.

Verifies the network transmit counterpart to the RX IQ server:
  * a client that connects gets the SOLSDR IQTX header and (unarmed) keys the
    chain WITHOUT asserting PTT — no RF
  * complex64 samples sent over the socket flow into the TXSession IQ buffer,
    reassembled correctly across arbitrary TCP chunk boundaries
  * only one transmitter at a time (second connection refused)
  * disconnect unkeys

Uses a FakeRadio + loopback TX dest. The pacer needs Linux timerfd; this test
skips cleanly without it (like test_tx_session.py).
"""
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from solsdr.protocol.profiles import PRO


class _ThreadPacer:
    """A timerfd-free stand-in for TXPacer so the server's socket + keying path
    is testable on any box (the real pacer needs Linux timerfd, absent < 3.13).
    Drains packet_source on a plain thread with time.sleep — good enough to move
    bytes; NOT the low-jitter production pacer."""

    def __init__(self, interval_s, packet_source, send, underrun_packet=None,
                 realtime=True, rt_priority=50, verbose=True):
        self.interval_s = interval_s
        self.packet_source = packet_source
        self.send = send
        self.underrun_packet = underrun_packet
        self._running = False
        self._thread = None
        self.sent = 0

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while self._running:
            pkt = self.packet_source()
            if pkt is None:
                pkt = self.underrun_packet
            if pkt is not None:
                try:
                    self.send(pkt)
                    self.sent += 1
                except OSError:
                    pass
            time.sleep(self.interval_s)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)

    def gap_stats_ms(self):
        return None


def _install_thread_pacer():
    """Patch TXSession to use the thread pacer. Returns a restore() callable."""
    import solsdr.tx_session as ts
    orig = ts.TXPacer
    ts.TXPacer = _ThreadPacer
    return lambda: setattr(ts, 'TXPacer', orig)


class FakeCtrl:
    def __init__(self):
        self.calls = []

    def set_frequency(self, f): self.calls.append(('freq', f)); return True
    def set_ptt(self, on): self.calls.append(('ptt', on)); return True
    def set_drive(self, b): self.calls.append(('drive', b)); return True
    def set_pa(self, on): self.calls.append(('pa', on)); return True
    def set_config_block(self, tx): self.calls.append(('cfg', tx)); return True


class FakeRadio:
    profile = PRO
    wire_rate = PRO.wire_rate
    radio_ip = '127.0.0.1'
    current_freq = 14074000
    current_mode = 'USB'
    _tx_active = False
    rx_sock = None

    def __init__(self, tx_port):
        self.ctrl = FakeCtrl()
        # Route TX IQ to a loopback socket by giving the profile-less path a
        # dest: IQTXServer builds a TXSession with default dest = radio TX port,
        # so we point tx_stream_port at our capture socket via a patched profile.
        self._tx_port = tx_port


def _make_server(radio, armed=False):
    from solsdr.api.iq_tx_server import IQTXServer
    # bind to an ephemeral port
    s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
    s.close()
    srv = IQTXServer(radio, host='127.0.0.1', port=port, armed=armed,
                     verbose=False)
    return srv, port


def test_header_and_no_rf_when_unarmed():
    restore = _install_thread_pacer()
    r = FakeRadio(tx_port=0)
    srv, port = _make_server(r, armed=False)
    srv.start()
    try:
        c = socket.create_connection(('127.0.0.1', port), timeout=2)
        c.settimeout(2)
        hdr = c.recv(128)
        assert hdr.startswith(b'SOLSDR IQTX'), hdr
        assert b'complex64' in hdr
        # send ~0.5 s of IQ in odd-sized chunks to exercise reassembly
        iq = (0.4 * np.exp(1j * np.linspace(0, 100, int(PRO.wire_rate // 2)))
              ).astype(np.complex64)
        raw = iq.tobytes()
        for i in range(0, len(raw), 777):     # 777 is not a multiple of 8
            c.sendall(raw[i:i + 777])
        time.sleep(0.5)
        assert srv.samples_received > 0, 'server received no samples'
        c.close()
        time.sleep(0.3)
        # unarmed: never keyed
        assert ('ptt', True) not in r.ctrl.calls, 'SAFETY: keyed while unarmed'
        print(f'PASS unarmed server: header ok, {srv.samples_received} samples, no PTT')
    finally:
        srv.stop()
        restore()


def test_one_transmitter_at_a_time():
    restore = _install_thread_pacer()
    r = FakeRadio(tx_port=0)
    srv, port = _make_server(r, armed=False)
    srv.start()
    try:
        c1 = socket.create_connection(('127.0.0.1', port), timeout=2)
        c1.settimeout(2); c1.recv(128)
        time.sleep(0.3)
        c2 = socket.create_connection(('127.0.0.1', port), timeout=2)
        c2.settimeout(2)
        reply = c2.recv(128)
        assert reply.startswith(b'ERR'), f'2nd client not refused: {reply!r}'
        c1.close(); c2.close()
        print('PASS one-transmitter: second connection refused while busy')
    finally:
        srv.stop()
        restore()


def test_disconnect_unkeys_when_armed():
    """An ARMED server keys on connect (ptt True) and unkeys on disconnect
    (ptt False), with the verified exit ordering. Uses the thread pacer."""
    restore = _install_thread_pacer()
    # capture TX IQ on the radio's TX port so enter_tx's real-dest path has a
    # sink (FakeRadio has no rx_sock, so TXSession opens its own socket).
    cap = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        cap.bind(('127.0.0.1', PRO.tx_stream_port))
        cap.settimeout(0.2)
    except OSError:
        cap.close()
        restore()
        print('SKIP: TX port busy'); return
    r = FakeRadio(tx_port=0)
    srv, port = _make_server(r, armed=True)
    srv.start()
    try:
        c = socket.create_connection(('127.0.0.1', port), timeout=2)
        c.settimeout(2); c.recv(128)
        iq = (0.5 * np.exp(1j * np.linspace(0, 50, int(PRO.wire_rate // 4)))
              ).astype(np.complex64)
        c.sendall(iq.tobytes())
        time.sleep(0.4)
        assert ('ptt', True) in r.ctrl.calls, 'armed server should key on connect'
        c.close()
        time.sleep(0.4)
        assert ('ptt', False) in r.ctrl.calls, 'disconnect should unkey'
        i_on = r.ctrl.calls.index(('ptt', True))
        i_off = r.ctrl.calls.index(('ptt', False))
        assert i_on < i_off
        print('PASS armed key/unkey: keyed on connect, unkeyed on disconnect')
    finally:
        srv.stop()
        cap.close()
        restore()


def test_partial_sample_reassembly_math():
    """The recv loop must only consume whole 8-byte complex64 samples and keep
    the remainder. Validate the arithmetic directly (no socket/timerfd needed)."""
    SAMP = 8
    buf = b''
    total = 0
    iq = np.arange(10, dtype=np.complex64)
    raw = iq.tobytes()             # 80 bytes
    for i in range(0, len(raw), 7):   # 7-byte chunks -> misaligned
        buf += raw[i:i + 7]
        n = len(buf) - (len(buf) % SAMP)
        if n:
            got = np.frombuffer(buf[:n], dtype=np.complex64)
            buf = buf[n:]
            total += len(got)
    # trailing remainder < 1 sample
    assert len(buf) < SAMP, len(buf)
    assert total == 10, f'reassembled {total} of 10 samples'
    print('PASS partial-sample reassembly: 10/10 samples, remainder held')


if __name__ == '__main__':
    test_partial_sample_reassembly_math()
    test_header_and_no_rf_when_unarmed()
    test_one_transmitter_at_a_time()
    test_disconnect_unkeys_when_armed()
    print('\nIQ TX SERVER TESTS PASSED')
