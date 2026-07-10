#!/usr/bin/env python3
"""Front-panel-mic + external-PTT packet parsing.

Covers the two wire formats reverse-engineered from the 2026-07-10 ExpertSDR3
captures (ARTEMISSDR.md §7-§8):
  * parse_ptt_edge      — the 0x1F/byte2=0x01 external-PTT edge packet
  * decode_tx_audio_packet — the downstream 0xFD mono mic-audio frame
  * the disambiguation between the PTT-edge packet and the streaming supply
    telemetry (both share opcode byte3=0x1F; only byte2 differs).
"""
import os
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solsdr.protocol import packet as pk

MAGIC = pk.MAGIC_PRO


def _ptt_edge_packet(pressed):
    """Synthesize the external-PTT edge packet: <magic> ff 01 1f ... state@18."""
    buf = bytearray(24)
    buf[0] = MAGIC
    buf[1] = 0xFF
    buf[2] = pk.PTT_EDGE_SUBTYPE   # 0x01 — the discriminator vs telemetry
    buf[3] = 0x1F
    buf[pk.PTT_EDGE_STATE_OFFSET] = 0x01 if pressed else 0x00
    return bytes(buf)


def _telemetry_packet(volts=13.6, amps=1.1, temp_c=41.0, fwd=0):
    """Synthesize the streaming supply telemetry: <magic> ff 00 1f ... (byte2=0)."""
    buf = bytearray(22)
    buf[0] = MAGIC
    buf[1] = 0xFF
    buf[2] = 0x00                  # streaming telemetry subtype
    buf[3] = 0x1F
    struct.pack_into('<H', buf, 8, fwd)
    struct.pack_into('<H', buf, 14, int(round(amps * 100)))
    struct.pack_into('<H', buf, 16, int(round(volts * 10)))
    struct.pack_into('<f', buf, 18, temp_c)
    return bytes(buf)


def _tx_audio_packet(mono):
    """Synthesize a downstream 0xFD mic-audio frame: 200 mono samples carried
    as I==Q duplicated pairs (the verified wire form)."""
    assert len(mono) == pk.IQ_SAMPLES_PER_PKT
    header = bytearray(pk.IQ_HDR_SIZE)
    header[0] = MAGIC
    header[1] = 0xFF
    header[2] = pk.OP_IQ_TX_ACTIVE     # 0xFD
    header[3] = 0xFF
    header[4:6] = struct.pack('<H', pk.IQ_PAYLOAD_SIZE)
    ints = np.rint(np.asarray(mono) * pk._FULL_SCALE).astype(np.int64)
    enc = pk._encode_24le_signed(ints)          # (200,3)
    payload = np.empty((pk.IQ_SAMPLES_PER_PKT, 6), dtype=np.uint8)
    payload[:, 0:3] = enc                       # Q
    payload[:, 3:6] = enc                       # I == Q (mono)
    return bytes(header) + payload.tobytes()


def test_ptt_edge():
    print("PTT edge parse...")
    assert pk.parse_ptt_edge(_ptt_edge_packet(True), MAGIC) is True
    assert pk.parse_ptt_edge(_ptt_edge_packet(False), MAGIC) is False
    # Not a PTT-edge packet:
    assert pk.parse_ptt_edge(_telemetry_packet(), MAGIC) is None      # byte2=0
    assert pk.parse_ptt_edge(b'\x01\xff\xfe\xff' + bytes(60), MAGIC) is None  # RX IQ hdr
    assert pk.parse_ptt_edge(b'', MAGIC) is None
    # Wrong magic:
    bad = bytearray(_ptt_edge_packet(True)); bad[0] = 0x32
    assert pk.parse_ptt_edge(bytes(bad), MAGIC) is None
    print("  OK")


def test_telemetry_not_confused_with_ptt():
    print("Telemetry vs PTT-edge disambiguation...")
    # The PTT-edge packet must NOT parse as telemetry (both are byte3=0x1F).
    assert pk.parse_telemetry(_ptt_edge_packet(True), MAGIC) is None
    assert pk.parse_telemetry(_ptt_edge_packet(False), MAGIC) is None
    # Real telemetry still parses, with correct fields.
    t = pk.parse_telemetry(_telemetry_packet(13.6, 1.1, 41.0), MAGIC)
    assert t is not None
    assert abs(t['voltage'] - 13.6) < 0.05, t['voltage']
    assert abs(t['current'] - 1.1) < 0.02, t['current']
    assert abs(t['temp_c'] - 41.0) < 0.1, t['temp_c']
    print("  OK")


def test_tx_audio_mono_roundtrip():
    print("Downstream mic audio decode (mono I==Q)...")
    t = np.linspace(0, 1, pk.IQ_SAMPLES_PER_PKT, endpoint=False)
    tone = (0.5 * np.sin(2 * np.pi * 3 * t)).astype(np.float32)
    out = pk.decode_tx_audio_packet(_tx_audio_packet(tone), MAGIC)
    assert out is not None
    assert out.shape == (pk.IQ_SAMPLES_PER_PKT,)
    assert out.dtype == np.float32
    # 24-bit quantization round-trip should be tight.
    assert np.max(np.abs(out - tone)) < 1e-3, np.max(np.abs(out - tone))
    # An RX IQ (0xFE) frame is NOT mic audio.
    rx = bytearray(_tx_audio_packet(tone)); rx[2] = pk.OP_IQ_RX_IDLE
    assert pk.decode_tx_audio_packet(bytes(rx), MAGIC) is None
    # Wrong length:
    assert pk.decode_tx_audio_packet(b'\x01\xff\xfd\xff', MAGIC) is None
    print("  OK")


if __name__ == '__main__':
    test_ptt_edge()
    test_telemetry_not_confused_with_ptt()
    test_tx_audio_mono_roundtrip()
    print("\nFRONT-PANEL TX PACKET TESTS PASSED")
