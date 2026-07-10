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

# External-PTT edge packet: radio->host on 50002. Header `<magic> ff 01 1f` —
# opcode 0x1F sits at byte 3 like the telemetry, but byte 2 is 0x01 here vs 0x00
# for the streaming supply telemetry (see parse_telemetry). That byte-2 subtype
# is the reliable discriminator; the two also differ in size (this one's UDP
# payload is ~22 bytes, telemetry ~34). Verified on a PRO 2026-07-10: the radio
# pushes one of these on every rear-panel PTT input edge, 3-13 ms before
# ExpertSDR3 issues its own key command. PTT state is at payload offset 18
# (0x01=pressed, 0x00=released).
PTT_EDGE_SUBTYPE = 0x01     # byte 2 (telemetry is 0x00)
PTT_EDGE_STATE_OFFSET = 18


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

    Single-receiver / back-compat helper: returns just the samples. Use
    decode_iq_packet_rx() when RX2 may be active and you need the receiver index.
    """
    result = decode_iq_packet_rx(packet, magic)
    return None if result is None else result[1]


def decode_iq_packet_rx(packet: bytes, magic: int = MAGIC_PRO):
    """
    Decode a 1210-byte RX IQ packet, returning (rx_index, samples).

    rx_index comes from RX IQ header byte 9: 0 = RX1, 1 = RX2 (verified on the
    PRO — both receivers stream interleaved on the same port, tagged by byte 9;
    byte 8 is the active-receiver count 1/2). Returns None if not a valid RX IQ
    frame. Samples are complex64, I + jQ, normalized to ~[-1, 1).
    """
    if len(packet) != IQ_PKT_SIZE:
        return None
    if packet[0] != magic or packet[1] != 0xFF or packet[2] != OP_IQ_RX_IDLE:
        return None

    rx_index = packet[9]  # 0 = RX1, 1 = RX2

    payload = np.frombuffer(packet, dtype=np.uint8, count=IQ_PAYLOAD_SIZE,
                            offset=IQ_HDR_SIZE)
    # Reshape into (200, 6): columns 0-2 = Q, 3-5 = I
    pairs = payload.reshape(IQ_SAMPLES_PER_PKT, IQ_BYTES_PER_SAMPLE)
    q = _decode_24le_signed(pairs[:, 0:3]).astype(np.float32) / _FULL_SCALE
    i = _decode_24le_signed(pairs[:, 3:6]).astype(np.float32) / _FULL_SCALE

    out = np.empty(IQ_SAMPLES_PER_PKT, dtype=np.complex64)
    out.real = i
    out.imag = q
    return rx_index, out


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

    The temperature field is read-only: the radio regulates its own fan
    autonomously in firmware (the fan cycles under solsdr, which sends no
    fan/temp command; a 2026-07-08 capture confirmed no host->radio fan/temp
    setpoint command exists in ExpertSDR3). There is nothing to send — just read
    temp_c/temp_f for display.

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
    # unlike the byte-2 opcode of the control packets. Byte 2 MUST be 0x00: the
    # external-PTT edge packet (parse_ptt_edge) shares byte3=0x1F but has byte2=
    # 0x01, and must not be misread as supply telemetry.
    if (len(buf) < 22 or buf[0] != magic or buf[1] != 0xFF
            or buf[2] != 0x00 or buf[3] != 0x1F):
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


def parse_ptt_edge(buf: bytes, magic: int = MAGIC_PRO):
    """Decode an external-PTT edge packet -> True (pressed) / False (released),
    or None if this isn't one.

    Header `<magic> ff 01 1f` (byte 2 = 0x01 distinguishes it from the byte2=0x00
    streaming telemetry that shares opcode 0x1F). PTT state is a byte at
    PTT_EDGE_STATE_OFFSET: 0x01 = pressed (key down), 0x00 = released (key up).
    Verified on a PRO 2026-07-10 — the radio emits one per rear-panel PTT edge.
    """
    if (len(buf) <= PTT_EDGE_STATE_OFFSET or buf[0] != magic or buf[1] != 0xFF
            or buf[2] != PTT_EDGE_SUBTYPE or buf[3] != 0x1F):
        return None
    return buf[PTT_EDGE_STATE_OFFSET] != 0x00


def decode_tx_audio_packet(packet: bytes, magic: int = MAGIC_PRO):
    """Decode a downstream 0xFD frame (radio->host) into a MONO float32 audio
    array, or None if it isn't a 0xFD frame.

    While keyed with a front-panel mic, the radio digitizes the selected mic and
    streams it DOWN in the SAME 1210-byte frame format as RX IQ but with opcode
    0xFD (TX-active) and both channels carrying the SAME mono sample (verified:
    I==Q on 100% of pairs, PRO 2026-07-10). We return the I channel (== Q) as
    real mono audio at the radio wire rate, normalized to ~[-1, 1). Feed it to
    the Modulator to transmit the operator's voice (host-side modulation).

    Distinct from decode_iq_packet_rx (opcode 0xFE, RX IQ). The header layout is
    identical otherwise, so we reuse the sample unpacking.
    """
    if len(packet) != IQ_PKT_SIZE:
        return None
    if packet[0] != magic or packet[1] != 0xFF or packet[2] != OP_IQ_TX_ACTIVE:
        return None
    payload = np.frombuffer(packet, dtype=np.uint8, count=IQ_PAYLOAD_SIZE,
                            offset=IQ_HDR_SIZE)
    pairs = payload.reshape(IQ_SAMPLES_PER_PKT, IQ_BYTES_PER_SAMPLE)
    # Mono: I == Q, so decode just the I channel (bytes 3-5) and return it real.
    i = _decode_24le_signed(pairs[:, 3:6]).astype(np.float32) / _FULL_SCALE
    return i
