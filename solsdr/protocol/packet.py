"""
Packet encode/decode utilities for the SunSDR2 protocol.

Vectorized with NumPy so the RX path can keep up with the radio's
~1562 packets/sec (312.5 kS/s) without a per-sample Python loop.

IQ wire format (verified against ArtemisSDR + live captures):
  - 1210-byte packet = 10-byte header + 1200-byte payload
  - 200 complex samples, 6 bytes each
  - Q first (bytes 0-2), I second (bytes 3-5)
  - 24-bit signed little-endian
  - Swapping Q/I mirrors the sideband, so order matters.
"""

import struct
import numpy as np

# Header / sizing constants (kept local so this module has no heavy imports)
IQ_PKT_SIZE = 1210
IQ_HDR_SIZE = 10
IQ_PAYLOAD_SIZE = 1200
IQ_SAMPLES_PER_PKT = 200
IQ_BYTES_PER_SAMPLE = 6
_FULL_SCALE = 8388608.0  # 2**23

MAGIC_PRO = 0x01
MAGIC_DX = 0x32
OP_IQ_RX_IDLE = 0xFE
OP_IQ_TX_ACTIVE = 0xFD

CTL_HDR_SIZE = 18


# --- 24-bit little-endian signed helpers (vectorized) ---------------------

def _decode_24le_signed(raw: np.ndarray) -> np.ndarray:
    """
    Decode an (N,3) uint8 array of little-endian 24-bit signed integers
    into an (N,) int32 array. Vectorized — no Python loop.
    """
    b0 = raw[:, 0].astype(np.int32)
    b1 = raw[:, 1].astype(np.int32)
    b2 = raw[:, 2].astype(np.int32)
    val = b0 | (b1 << 8) | (b2 << 16)
    # Sign-extend from 24 to 32 bits
    val = np.where(val & 0x800000, val - 0x1000000, val)
    return val


def _encode_24le_signed(vals: np.ndarray) -> np.ndarray:
    """
    Encode an (N,) int array into an (N,3) uint8 array of little-endian
    24-bit signed integers. Values are clipped to the 24-bit signed range.
    """
    v = np.clip(vals, -0x800000, 0x7FFFFF).astype(np.int32)
    v &= 0xFFFFFF  # two's-complement wrap into 24 bits
    out = np.empty((v.shape[0], 3), dtype=np.uint8)
    out[:, 0] = v & 0xFF
    out[:, 1] = (v >> 8) & 0xFF
    out[:, 2] = (v >> 16) & 0xFF
    return out


# --- RX decode -------------------------------------------------------------

def decode_iq_packet(packet: bytes, magic: int = MAGIC_PRO):
    """
    Decode a 1210-byte RX IQ packet to a complex64 array of 200 samples.

    Returns None if the packet is not a valid RX IQ frame. The returned
    samples are normalized to roughly [-1, 1) as I + jQ.
    """
    if len(packet) != IQ_PKT_SIZE:
        return None
    if packet[0] != magic or packet[1] != 0xFF or packet[2] != OP_IQ_RX_IDLE:
        return None

    payload = np.frombuffer(packet, dtype=np.uint8, count=IQ_PAYLOAD_SIZE,
                            offset=IQ_HDR_SIZE)
    # Reshape into (200, 6): columns 0-2 = Q, 3-5 = I
    pairs = payload.reshape(IQ_SAMPLES_PER_PKT, IQ_BYTES_PER_SAMPLE)
    q = _decode_24le_signed(pairs[:, 0:3]).astype(np.float32) / _FULL_SCALE
    i = _decode_24le_signed(pairs[:, 3:6]).astype(np.float32) / _FULL_SCALE

    out = np.empty(IQ_SAMPLES_PER_PKT, dtype=np.complex64)
    out.real = i
    out.imag = q
    return out


# --- TX encode -------------------------------------------------------------

def encode_iq_packet(iq: np.ndarray, seq: int, magic: int = MAGIC_PRO) -> bytes:
    """
    Encode 200 complex samples into a 1210-byte TX IQ packet.

    iq: complex array, len 200, normalized to [-1, 1] (I=real, Q=imag).
    seq: 16-bit sequence counter.
    """
    if len(iq) != IQ_SAMPLES_PER_PKT:
        raise ValueError(f"TX packet needs {IQ_SAMPLES_PER_PKT} samples, got {len(iq)}")

    header = bytearray(IQ_HDR_SIZE)
    header[0] = magic
    header[1] = 0xFF
    header[2] = OP_IQ_TX_ACTIVE
    header[3] = 0xFF
    header[4:6] = struct.pack('<H', IQ_PAYLOAD_SIZE)
    header[6:8] = struct.pack('<H', seq & 0xFFFF)
    header[8] = 0x02
    header[9] = 0x01

    q_int = np.rint(np.asarray(iq.imag) * _FULL_SCALE).astype(np.int64)
    i_int = np.rint(np.asarray(iq.real) * _FULL_SCALE).astype(np.int64)

    payload = np.empty((IQ_SAMPLES_PER_PKT, IQ_BYTES_PER_SAMPLE), dtype=np.uint8)
    payload[:, 0:3] = _encode_24le_signed(q_int)
    payload[:, 3:6] = _encode_24le_signed(i_int)

    return bytes(header) + payload.tobytes()


# --- Control packet helpers ------------------------------------------------

def build_control_packet(opcode: int, payload: bytes = b'', magic: int = MAGIC_PRO) -> bytes:
    """Build an 18-byte-header control packet."""
    header = bytearray(CTL_HDR_SIZE)
    header[0] = magic
    header[1] = 0xFF
    header[2] = opcode
    header[3] = 0x00
    header[4:6] = struct.pack('<H', len(payload))
    header[10] = 0x01
    return bytes(header) + payload


def build_discovery_probe(family: int) -> bytes:
    """
    24-byte SunSDR discovery/wake probe: <family> ff 00 1a + one's-complement
    checksum over bytes 0..21. Broadcast to <subnet>.255:50001 and
    255.255.255.255:50001. Verified byte-identical to ExpertSDR3's probe.
    """
    pkt = bytearray(24)
    pkt[0] = family
    pkt[1] = 0xFF
    pkt[2] = 0x00
    pkt[3] = 0x1A
    s = 0
    for i in range(0, 22, 2):
        s += pkt[i] | (pkt[i + 1] << 8)
        if s & 0x10000:
            s = (s & 0xFFFF) + 1
    ck = (~s) & 0xFFFF
    pkt[22] = ck & 0xFF
    pkt[23] = (ck >> 8) & 0xFF
    return bytes(pkt)


def is_discovery_reply(buf: bytes) -> bool:
    """True if buf is a SunSDR discovery reply (XX ff 01 1a)."""
    return len(buf) >= 24 and buf[1] == 0xFF and buf[2] == 0x01 and buf[3] == 0x1A


def parse_discovery_reply(buf: bytes):
    """Return (ip_str, control_port) from a discovery reply, or None."""
    if not is_discovery_reply(buf):
        return None
    ip = '.'.join(str(b) for b in buf[10:14])
    port = buf[18] | (buf[19] << 8)
    if port == 0:
        port = 50001
    return ip, port


def parse_telemetry(buf: bytes, magic: int = MAGIC_PRO):
    """Decode a 0x1F periodic status packet -> dict of supply telemetry.

    Field map VERIFIED 2026-07-07 against the radio's own display (13.6 V,
    ~1.1 A, ~41 C) on a real PRO. Little-endian:
        offset  8: uint16  FORWARD POWER (0 at RX, rises with TX output)
        offset 10: uint16  supply current x ~455 (redundant copy of offset 14)
        offset 12: uint16  fixed reference, ~4088 (12-bit full-scale marker)
        offset 14: uint16  supply current, /100  -> amps
        offset 16: uint16  supply voltage, /10   -> volts
        offset 18: float32 temperature (deg C)

    Offsets 8/10/12 fully characterised 2026-07-07 via a keyed drive sweep into
    a dummy load (tools/tx_telem_probe.py):
      * offset 8  = forward-power indicator: exactly 0 during RX, monotonic with
        TX drive/output. Nonlinear (not a clean W or sqrt-W scale from 20m data
        alone) and there is NO companion reflected-power field, so the radio
        does NOT report SWR here — ExpertSDR3's SWR must come from elsewhere.
        Usable as a rough built-in "is it making power" flag; needs multi-band
        keyed cal to convert to watts.
      * offset 10 = supply current again: off10/off14 is a dead-constant 455
        across every drive level. Redundant with offset 14, not S-meter, not SWR.
      * offset 12 = fixed ~4088 reference (12-bit full-scale marker), never moves.
    None of these is the S-meter (that's GUI-computed from IQ; use
    demod.s_meter). Returns None if not a telemetry frame or too short.
    """
    # Header is <magic> ff 00 1f — opcode 0x1F sits at byte 3 (byte 2 is 0x00),
    # unlike the byte-2 opcode of the control packets.
    if len(buf) < 22 or buf[0] != magic or buf[1] != 0xFF or buf[3] != 0x1F:
        return None
    fwd_raw = struct.unpack('<H', buf[8:10])[0]
    current = struct.unpack('<H', buf[14:16])[0] / 100.0
    voltage = struct.unpack('<H', buf[16:18])[0] / 10.0
    temp_c = struct.unpack('<f', buf[18:22])[0]
    return {
        'voltage': voltage,
        'current': current,
        'temp_c': temp_c,
        'temp_f': temp_c * 9.0 / 5.0 + 32.0,
        # Forward-power indicator: 0 at RX, rises with TX output. Raw (uncal).
        'fwd_power_raw': fwd_raw,
    }
