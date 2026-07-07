"""
Mock SunSDR2 PRO radio for offline development and testing.

Emulates enough of the protocol to exercise the whole client stack without
hardware:
  - Answers discovery/wake broadcasts (XX ff 00 1a) with a discovery reply.
  - ACKs control commands on port 50001 (freq, mode, PTT, keepalive, power).
  - Streams synthetic RX IQ (a tone at a configurable audio offset) on 50002
    at the real 1562 packets/sec cadence once STATE_SYNC + frequency arrive.

This is intentionally a behavioral mock, not a bit-exact firmware emulator.
It reproduces the framing and timing the client depends on so the RX DSP,
control API, and Hamlib layers can be validated end-to-end.

Run standalone:
    python3 -m solsdr.mock_radio            # binds real ports (needs them free)
    python3 -m solsdr.mock_radio --ip 127.0.0.1
"""

import argparse
import socket
import struct
import threading
import time
import numpy as np

from .protocol import packet as pk

CONTROL_PORT = 50001
RX_STREAM_PORT = 50002
RADIO_RATE = 312500.0
SAMPLES_PER_PKT = pk.IQ_SAMPLES_PER_PKT
PKT_INTERVAL = SAMPLES_PER_PKT / RADIO_RATE  # ~640 us


class MockRadio:
    def __init__(self, bind_ip='0.0.0.0', client_ip='127.0.0.1',
                 radio_ip='10.1.2.3', tone_hz=1000.0, magic=pk.MAGIC_PRO,
                 verbose=True):
        self.bind_ip = bind_ip
        self.client_ip = client_ip
        self.radio_ip = radio_ip
        self.tone_hz = tone_hz
        self.magic = magic
        self.verbose = verbose

        self.ctrl_sock = None
        self.running = False
        self.powered = False
        self.streaming = False
        self.freq_hz = 0
        self.ptt = False

        self._stream_dest = None       # (ip, port) learned from client
        self._phase = 0.0
        self._seq = 0
        self._threads = []

    # -- logging -----------------------------------------------------------
    def _log(self, *a):
        if self.verbose:
            print('[mock]', *a)

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self.ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ctrl_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ctrl_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.ctrl_sock.bind((self.bind_ip, CONTROL_PORT))
        self.ctrl_sock.settimeout(0.5)
        self.running = True
        t = threading.Thread(target=self._control_loop, daemon=True)
        t.start()
        self._threads.append(t)
        self._log(f'listening on {self.bind_ip}:{CONTROL_PORT}, '
                  f'radio_ip={self.radio_ip}, tone={self.tone_hz} Hz')

    def stop(self):
        self.running = False
        self.streaming = False
        for t in self._threads:
            t.join(timeout=1)
        if self.ctrl_sock:
            self.ctrl_sock.close()

    # -- control -----------------------------------------------------------
    def _control_loop(self):
        while self.running:
            try:
                data, addr = self.ctrl_sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle_control(data, addr)

    def _handle_control(self, data, addr):
        # Discovery/wake probe?
        if len(data) >= 4 and data[1] == 0xFF and data[2] == 0x00 and data[3] == 0x1A:
            self._send_discovery_reply(addr)
            return
        if len(data) < pk.CTL_HDR_SIZE:
            return
        opcode = data[2]
        payload = data[pk.CTL_HDR_SIZE:]

        if opcode == 0x18:                       # keepalive
            self._ack(opcode, addr)
        elif opcode == 0x02:                     # power off
            self._log('power off')
            self.powered = False
            self.streaming = False
            self._ack(opcode, addr)
        elif opcode == 0x01:                     # STATE_SYNC (also power-on)
            self.powered = True
            self._ack(opcode, addr)
            # STATE_SYNC starts streaming once we know where to send it.
            self._maybe_start_stream(addr)
        elif opcode == 0x09:                     # primary freq
            if len(payload) >= 8:
                # payload layout: 8 bytes header-ish then u64? Client packs
                # freq*10 as the trailing 8 bytes in some builds; accept the
                # last 8 bytes as the scaled freq.
                scaled = struct.unpack('<Q', payload[-8:])[0]
                self.freq_hz = scaled // 10
                self._log(f'set freq {self.freq_hz} Hz')
            self._ack(opcode, addr)
            self._maybe_start_stream(addr)
        elif opcode == 0x08:                     # companion freq
            self._ack(opcode, addr)
        elif opcode == 0x06:                     # PTT
            self.ptt = bool(payload and payload[0])
            self._log(f'PTT {"ON" if self.ptt else "OFF"}')
            self._ack(opcode, addr)
        elif opcode == 0x20:                     # mode / config block
            self._ack(opcode, addr)
        else:
            self._ack(opcode, addr)

    def _ack(self, opcode, addr):
        # Minimal ACK: echo header with same opcode, empty payload.
        self.ctrl_sock.sendto(pk.build_control_packet(opcode, b'', self.magic), addr)

    def _send_discovery_reply(self, addr):
        buf = bytearray(24)
        buf[0] = self.magic
        buf[1] = 0xFF
        buf[2] = 0x01
        buf[3] = 0x1A
        # IP big-endian at offset 10
        octets = [int(x) for x in self.radio_ip.split('.')]
        buf[10:14] = bytes(octets)
        # control port LE at offset 18
        buf[18:20] = struct.pack('<H', CONTROL_PORT)
        self.ctrl_sock.sendto(bytes(buf), addr)
        self._log(f'discovery reply -> {addr}')

    # -- RX stream ---------------------------------------------------------
    def _maybe_start_stream(self, addr):
        if self.streaming:
            return
        # Stream to the client IP on the RX stream port.
        self._stream_dest = (self.client_ip, RX_STREAM_PORT)
        self.streaming = True
        t = threading.Thread(target=self._stream_loop, daemon=True)
        t.start()
        self._threads.append(t)
        self._log(f'RX stream -> {self._stream_dest} '
                  f'(tone {self.tone_hz} Hz @ {RADIO_RATE/1000:.1f} kS/s)')

    def _stream_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dphi = 2 * np.pi * self.tone_hz / RADIO_RATE
        # Send in small bursts to keep cadence without spinning the CPU.
        next_t = time.perf_counter()
        while self.running and self.streaming:
            n = SAMPLES_PER_PKT
            idx = np.arange(n)
            phase = self._phase + dphi * idx
            iq = (0.5 * np.exp(1j * phase)).astype(np.complex64)
            self._phase = float((self._phase + dphi * n) % (2 * np.pi))
            pkt = pk.encode_iq_packet(iq, self._seq, self.magic)
            # Re-stamp opcode to RX_IDLE (0xFE) so the client's RX path accepts it.
            pkt = bytes([self.magic, 0xFF, 0xFE]) + pkt[3:]
            self._seq = (self._seq + 1) & 0xFFFF
            try:
                sock.sendto(pkt, self._stream_dest)
            except OSError:
                break
            next_t += PKT_INTERVAL
            sleep = next_t - time.perf_counter()
            if sleep > 0:
                time.sleep(sleep)
            elif sleep < -0.1:
                next_t = time.perf_counter()  # fell behind; resync
        sock.close()


def main():
    ap = argparse.ArgumentParser(description='Mock SunSDR2 PRO radio')
    ap.add_argument('--bind', default='0.0.0.0', help='bind IP for control socket')
    ap.add_argument('--client', default='127.0.0.1', help='client IP to stream IQ to')
    ap.add_argument('--radio-ip', default='10.1.2.3', help='IP to report in discovery reply')
    ap.add_argument('--tone', type=float, default=1000.0, help='RX tone offset Hz')
    args = ap.parse_args()

    radio = MockRadio(bind_ip=args.bind, client_ip=args.client,
                      radio_ip=args.radio_ip, tone_hz=args.tone)
    radio.start()
    print('Mock radio running. Ctrl-C to stop.')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        radio.stop()
        print('\nstopped')


if __name__ == '__main__':
    main()
