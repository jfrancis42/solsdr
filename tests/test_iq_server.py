#!/usr/bin/env python3
"""Test IQ streaming server end-to-end with the mock radio."""
import os, socket, struct, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from solsdr.api.iq_server import IQStreamServer
from solsdr.protocol import packet as pk


def test_iq_server_roundtrip():
    srv = IQStreamServer(host='127.0.0.1', port=15599, verbose=False)
    srv.start(rate=39062.5, freq=14074000)
    time.sleep(0.3)

    # Connect a client
    c = socket.socket(); c.connect(('127.0.0.1', 15599)); c.settimeout(2)
    header = b''
    while b'\n' not in header:
        header += c.recv(1)
    assert header.startswith(b'SOLSDR IQ'), header
    assert b'rate=39062.5' in header and b'complex64' in header
    time.sleep(0.2)  # ensure server registered the client

    # Publish a known tone
    n = 2000
    t = np.arange(n) / 39062.5
    tone = (0.5 * np.exp(2j * np.pi * 1000 * t)).astype(np.complex64)
    for _ in range(10):
        srv.publish(tone)
        time.sleep(0.01)

    # Read it back
    buf = b''
    deadline = time.time() + 2
    while len(buf) < n * 8 * 5 and time.time() < deadline:
        try:
            buf += c.recv(65536)
        except socket.timeout:
            break
    got = np.frombuffer(buf[:len(buf) // 8 * 8], dtype=np.complex64)
    assert len(got) >= n, f'only got {len(got)} samples'
    # Verify it's the 1000 Hz tone
    seg = got[:n]
    fft = np.fft.fftshift(np.fft.fft(seg))
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1 / 39062.5))
    peak = freqs[np.argmax(np.abs(fft))]
    assert abs(peak - 1000) < 50, f'tone at {peak} Hz, expected 1000'
    print(f'PASS: IQ server round-trip, tone recovered at {peak:.0f} Hz, '
          f'{len(got)} samples through TCP')

    c.close()
    srv.stop()


if __name__ == '__main__':
    test_iq_server_roundtrip()
    print('\nIQ SERVER TEST PASSED')
