"""RX IQ Stream Receiver - Handles incoming IQ packets from radio"""

import socket
import struct
import numpy as np
from typing import Callable, Optional
import threading

from . import packet as _pk


class RXStreamReceiver:
    """Receives and decodes IQ packets from SunSDR2"""

    def __init__(self, callback: Callable[[np.ndarray], None], port: int = 50002):
        """
        Initialize RX stream receiver

        Args:
            callback: Function to call with decoded IQ samples (complex64 array)
            port: UDP port to listen on (50002 for PRO RX)
        """
        self.callback = callback
        self.port = port

        self.sock = None
        self.running = False
        self.thread = None

        # Statistics
        self.packets_received = 0
        self.packets_dropped = 0
        self.bytes_received = 0

    def start(self):
        """Start receiving IQ packets"""
        if self.running:
            return

        # Create and bind socket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Set receive buffer size (handle burst traffic)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)

        self.sock.bind(('', self.port))
        self.sock.settimeout(1.0)

        # Start receive thread
        self.running = True
        self.thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.thread.start()

    def stop(self):
        """Stop receiving"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        if self.sock:
            self.sock.close()

    def _receive_loop(self):
        """Main receive loop (runs in thread)"""
        while self.running:
            try:
                data, addr = self.sock.recvfrom(2048)

                # Decode packet (vectorized; ~7x faster than the pure-Python
                # per-sample loop in _decode_packet, which is retained for
                # tests and reference).
                samples = _pk.decode_iq_packet(data)
                if samples is not None:
                    self.packets_received += 1
                    self.bytes_received += len(data)

                    # Call user callback
                    try:
                        self.callback(samples)
                    except Exception as e:
                        print(f"Callback error: {e}")
                else:
                    self.packets_dropped += 1

            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"RX error: {e}")

    def _decode_packet(self, packet: bytes) -> Optional[np.ndarray]:
        """
        Decode IQ packet to complex64 samples

        Packet format:
          [0-1]: Magic (0x01 0xFF)
          [2]: Opcode (0xFE = RX_IDLE)
          [3]: 0x00
          [4-5]: Payload length (should be 0x04B0 = 1200)
          [6-9]: Sequence/flags
          [10+]: IQ data (1200 bytes)

        IQ format:
          Each sample: 6 bytes
          Bytes 0-2: Q (24-bit signed little-endian)
          Bytes 3-5: I (24-bit signed little-endian)

        Args:
            packet: Raw UDP packet bytes

        Returns:
            numpy array of 200 complex64 samples, or None if invalid
        """
        if len(packet) != 1210:
            return None

        # Check header
        if packet[0] != 0x01 or packet[1] != 0xFF:
            return None

        # Opcode should be 0xFE (RX_IDLE)
        if packet[2] != 0xFE:
            return None

        # Extract payload (skip 10-byte header)
        payload = packet[10:]
        if len(payload) != 1200:
            return None

        # Decode samples
        samples = np.empty(200, dtype=np.complex64)

        for i in range(200):
            offset = i * 6

            # Q is bytes 0-2 (little-endian 24-bit signed)
            q_bytes = payload[offset:offset+3]
            # Sign-extend to 32-bit
            if q_bytes[2] & 0x80:  # Negative
                q_int = struct.unpack('<i', q_bytes + b'\xff')[0]
            else:
                q_int = struct.unpack('<i', q_bytes + b'\x00')[0]

            # I is bytes 3-5
            i_bytes = payload[offset+3:offset+6]
            if i_bytes[2] & 0x80:
                i_int = struct.unpack('<i', i_bytes + b'\xff')[0]
            else:
                i_int = struct.unpack('<i', i_bytes + b'\x00')[0]

            # Normalize to -1.0 to +1.0 (24-bit range: -8388608 to 8388607)
            i_float = i_int / 8388608.0
            q_float = q_int / 8388608.0

            # Create complex sample (I + jQ)
            samples[i] = complex(i_float, q_float)

        return samples

    def get_stats(self) -> dict:
        """Get receiver statistics"""
        return {
            'packets_received': self.packets_received,
            'packets_dropped': self.packets_dropped,
            'bytes_received': self.bytes_received,
            'packet_rate': self.packets_received / max(1, self.bytes_received / 1210),
        }
