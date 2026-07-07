#!/usr/bin/env python3
"""Test IQ packet decoding"""

import sys
import struct
import numpy as np

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solsdr.protocol.rx_stream import RXStreamReceiver


def test_decode_packet():
    """Test decoding of IQ packet"""
    print("Testing IQ packet decode...")

    # Create a synthetic IQ packet
    # Header: 10 bytes
    header = bytearray(10)
    header[0] = 0x01  # Magic
    header[1] = 0xFF
    header[2] = 0xFE  # RX_IDLE opcode
    header[4:6] = struct.pack('<H', 1200)  # Payload length

    # Payload: 1200 bytes (200 samples × 6 bytes)
    payload = bytearray(1200)

    # Create test samples: simple sine wave
    # Q and I with known values
    for i in range(200):
        # Simple pattern: Q = i, I = -i
        q_val = i
        i_val = -i

        # Pack as 24-bit little-endian
        offset = i * 6

        # Q (bytes 0-2)
        q_bytes = q_val.to_bytes(3, 'little', signed=True)
        payload[offset:offset+3] = q_bytes

        # I (bytes 3-5)
        i_bytes = i_val.to_bytes(3, 'little', signed=True)
        payload[offset+3:offset+6] = i_bytes

    # Complete packet
    packet = bytes(header) + bytes(payload)

    assert len(packet) == 1210, f"Packet length {len(packet)}, expected 1210"

    # Decode
    receiver = RXStreamReceiver(callback=lambda x: None)
    samples = receiver._decode_packet(packet)

    assert samples is not None, "Decode returned None"
    assert len(samples) == 200, f"Got {len(samples)} samples, expected 200"
    assert samples.dtype == np.complex64, f"Wrong dtype: {samples.dtype}"

    # Check first few samples
    for i in range(5):
        expected_i = -i / 8388608.0
        expected_q = i / 8388608.0

        actual = samples[i]

        # Allow small floating point error
        assert abs(actual.real - expected_i) < 1e-6, \
            f"Sample {i} I: got {actual.real}, expected {expected_i}"
        assert abs(actual.imag - expected_q) < 1e-6, \
            f"Sample {i} Q: got {actual.imag}, expected {expected_q}"

    print("✓ IQ decode test passed")
    print(f"  First 5 samples: {samples[:5]}")


def test_invalid_packets():
    """Test rejection of invalid packets"""
    print("\nTesting invalid packet rejection...")

    receiver = RXStreamReceiver(callback=lambda x: None)

    # Wrong length
    packet = bytes(1000)
    assert receiver._decode_packet(packet) is None, "Should reject wrong length"

    # Wrong magic
    packet = bytearray(1210)
    packet[0] = 0xFF  # Wrong magic
    packet[1] = 0xFF
    packet[2] = 0xFE
    assert receiver._decode_packet(bytes(packet)) is None, "Should reject wrong magic"

    # Wrong opcode
    packet[0] = 0x01  # Fix magic
    packet[2] = 0x00  # Wrong opcode
    assert receiver._decode_packet(bytes(packet)) is None, "Should reject wrong opcode"

    print("✓ Invalid packet rejection test passed")


def test_real_packet():
    """Test with a real captured packet"""
    print("\nTesting with real packet data...")

    # This is from actual capture (first 40 bytes shown in testing)
    # 01fffeffb00413000100020000feffff020000fafffffcffff060000010000030000fdffff000000
    hex_data = '01fffeffb00413000100020000feffff020000fafffffcffff060000010000030000fdffff000000'

    # Pad to full packet (this is just first 40 bytes, rest would be more IQ data)
    # For test, we'll create full packet with this header
    header = bytes.fromhex(hex_data)

    # Create fake rest of packet
    payload = bytearray(1200)
    payload[:len(header)-10] = header[10:]  # Copy what we have

    packet = header[:10] + bytes(payload)

    receiver = RXStreamReceiver(callback=lambda x: None)
    samples = receiver._decode_packet(packet)

    assert samples is not None, "Real packet decode failed"
    assert len(samples) == 200, "Wrong sample count"

    print("✓ Real packet test passed")
    print(f"  First sample: {samples[0]}")
    print(f"  RMS: {np.sqrt(np.mean(np.abs(samples)**2)):.6f}")


if __name__ == '__main__':
    print("="*70)
    print("IQ Packet Decode Tests")
    print("="*70)

    try:
        test_decode_packet()
        test_invalid_packets()
        test_real_packet()

        print("\n" + "="*70)
        print("All tests passed! ✓")
        print("="*70)

    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
