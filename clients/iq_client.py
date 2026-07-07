#!/usr/bin/env python3
"""
Example IQ stream client: connects to the solsdr IQ server, reads the header,
and consumes complex64 samples. Demonstrates the "GNU Radio is a client"
pattern; a GNU Radio flowgraph would use a TCP source (complex float) pointed
at the same host:port (skip the one-line header).

Usage:
    python3 clients/iq_client.py [host] [port] [--seconds N]
"""
import argparse
import socket
import sys
import time

import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('host', nargs='?', default='127.0.0.1')
    ap.add_argument('port', nargs='?', type=int, default=5555)
    ap.add_argument('--seconds', type=float, default=5)
    args = ap.parse_args()

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((args.host, args.port))
    s.settimeout(3)

    # Read the one-line text header
    header = b''
    while b'\n' not in header:
        chunk = s.recv(1)
        if not chunk:
            print('server closed before header'); sys.exit(1)
        header += chunk
    print('header:', header.decode().strip())

    # Consume complex64 samples
    buf = b''
    total = 0
    t0 = time.time()
    powers = []
    while time.time() - t0 < args.seconds:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
        n = len(buf) // 8  # 8 bytes per complex64
        if n:
            iq = np.frombuffer(buf[:n * 8], dtype=np.complex64)
            buf = buf[n * 8:]
            total += n
            powers.append(np.mean(np.abs(iq) ** 2))
    s.close()
    dur = time.time() - t0
    rate = total / dur if dur else 0
    avg_pwr = 10 * np.log10(np.mean(powers) + 1e-15) if powers else -999
    print(f'received {total} complex samples in {dur:.1f}s = {rate:.0f} S/s')
    print(f'mean IQ power {avg_pwr:.1f} dB')


if __name__ == '__main__':
    main()
