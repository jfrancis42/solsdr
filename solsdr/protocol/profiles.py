"""
Radio variant profiles for the SunSDR2 family.

All variant-specific constants live here so the rest of the code is
variant-agnostic. Adding the DX later means filling in / verifying the DX
profile and nothing else.

PRO values are VERIFIED against real hardware (2026-07-06, decoded live FT8).
DX values are fully populated from the ArtemisSDR source (magic, ports, rate,
DDC offset, and the complete power-on/init sequence) but are NOT verified
against real hardware by this project. The DX profile carries verified=False;
treat it as a starting point to validate on a real DX, not a known-good config.
Note the DX macro in ArtemisSDR was authored against real DX hardware, so it is
more trustworthy than their PRO extrapolation proved to be — but "more likely
correct" is not "tested," so the flag stays False until someone runs it.
"""
from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class RadioProfile:
    name: str
    magic: int                 # header byte 0
    control_port: int          # UDP control (both variants: 50001)
    rx_stream_port: int        # UDP RX IQ
    tx_stream_port: int        # UDP TX IQ
    wire_rate: float           # native IQ sample rate (Hz)
    ddc0_offset_hz: int        # PRIMARY(0x09) = display - offset; COMP(0x08)=display
    discovery_family: int      # family byte in the discovery probe
    # Verified init sequence (full wire packets, hex). Empty => must be captured.
    init_sequence: List[str] = field(default_factory=list)
    # Whether the RX stream requires the client to echo a silence packet per
    # RX packet to stay alive (verified True on PRO).
    rx_needs_tx_keepalive: bool = True
    verified: bool = False


# --- Verified init sequence for the PRO (from expert_14074.pcapng) ----------
# Kept here as the single source of truth; poweron_pro.py re-exports it.
_PRO_INIT = [
    "01ff1d00040000000000010000000000000001000000",
    "01ff1b00040000000000010000000000000000000000",
    "01ff0500040000000000010000000000000002000000",
    "01ff1900040000000000010000000000000072000000",
    "01ff2100040000000000010000000000000000000000",
    "01ff01003200000000000100000000000000140000001400000014000000140000001400000014000000140000001400000000000000010000000000000000000000c008",
    "01ff1700040000000000010000000000000080000000",
    "01ff1e00040000000000010000000000000000000000",
    "01ff1500040000000000010000000000000000000000",
    "01ff07001a00000000000100000000000000000000000000000000000000007f7f7f7f7f7f7f7f7f7f7f7f7f",
    "01ff2400040000000000010000000000000001000000",
    "01ff20003400000000000100000000000000010000000100000001000000000000006400000000000000000000001e000000bc02000007000000640000002c01000064000000",
    "01ff2600040000000000010000000000000000000000",
    "01ff27001000000000000100000000000000dc460300b6d20000dc460300b6d20000",
    "01ff22000c00000000000100000000000000000000000084d71700000000",
]

# PRO sample-rate ladder. VERIFIED 2026-07-07 by capturing ExpertSDR3 stepping
# through its rates: the rate is selected by a "rate index" at byte offsets 56
# and 58 (both uint16 LE, same value) of the STATE_SYNC (0x01) packet — NOT by
# the eight 0x14 rate-code words, which stay constant (that was ArtemisSDR's
# mistaken assumption in issue #47). Index 0..3 -> 39062.5 * 2**index.
PRO_RATE_INDEX = {39062.5: 0, 78125.0: 1, 156250.0: 2, 312500.0: 3}
# byte offset (into the STATE_SYNC packet) of the two rate-index uint16 fields
PRO_RATE_INDEX_OFFSETS = (56, 58)

PRO = RadioProfile(
    name="SunSDR2 PRO",
    magic=0x01,
    control_port=50001,
    rx_stream_port=50002,
    tx_stream_port=50002,       # VERIFIED: TX IQ (0xFD) goes to 50002, NOT 50003
                                # — same bidirectional port as RX. ExpertSDR3
                                # sends 0xFD on 50002 in place of the 0xFE idle
                                # keepalives while keyed. (50003 gave no output.)
    wire_rate=39062.5,          # VERIFIED default (rate index 0)
    ddc0_offset_hz=0,           # VERIFIED: 92.5 kHz (DX value) gave only noise
    discovery_family=0x01,
    init_sequence=_PRO_INIT,
    rx_needs_tx_keepalive=True,
    verified=True,
)

# --- DX power-on / init sequence (from ArtemisSDR power_on_macro_dx[]) -------
# Transcribed verbatim from ArtemisSDR ChannelMaster/sunsdr.c power_on_macro_dx
# (the wire packets, in order; the C table's length/delay columns are dropped —
# we ACK-gate each send instead of using fixed delays). Unlike the PRO, the DX
# macro was authored by K0KOZ against real DX hardware, so it is more likely
# correct than the PRO extrapolation was — but this project has NOT tested it
# against a DX, so the DX profile remains verified=False.
#
# Notable DX-specific steps not present in the PRO sequence:
#   * 0x5f POWER_WAKE x3 (the DX power-on handshake)
#   * 0x5a STATE_REPEAT priming steps
#   * a different 0x01 STATE_SYNC template (0x32 payload words, not 0x14)
_DX_INIT = [
    "32ff5a000000000000000100000000000000",
    "32ff1800040000000000010000000000000000000000",
    "32ff0e000000000000000100000000000000",
    "32ff1800040000000000010000000000000000000000",
    "32ff5f000600000000000100000000000000000000000000",
    "32ff5f000600000000000100000000000000000000000000",
    "32ff5f000600000000000100000000000000000000000000",
    "32ff1d00040000000000010000000000000000000000",
    "32ff1b00040000000000010000000000000000000000",
    "32ff0500040000000000010000000000000083000000",
    "32ff1800040000000000010000000000000000000000",
    "32ff19000400000000000100000000000000ff000000",
    "32ff2100040000000000010000000000000001000000",
    "32ff5a000000000000000100000000000000",
    "32ff5a000000000000000100000000000000",
    "32ff5a000000000000000100000000000000",
    "32ff5a000000000000000100000000000000",
    "32ff5a000000000000000100000000000000",
    "32ff010032000000000001000000000000003200000032000000320000003200000032000000320000003200000032000000000000000100030003008700f87f00002879",
    "32ff0900080000000000010000000000000060fd4d0400000000",
    "32ff0800080000000000010000000000000098f35b0400000000",
    "32ff08000800010000000100000000000000c058510400000000",
    "32ff17000400000000000100000000000000f5000000",
    "32ff1e00040000000000010000000000000000000000",
    "32ff1500040000000000010000000000000001000000",
    "32ff07001a000000000001000000000000000000000000000000000000000000000000000000000000000000",
    "32ff2400040000000000010000000000000000000000",
    "32ff20003400000000000100000000000000010000000100000000000000000000006400000000000000000000001e000000bc02000007000000640000002c01000064000000",
    "32ff1800040000000000010000000000000000000000",
    "32ff2600040000000000010000000000000000000000",
    "32ff27001000000000000100000000000000dc460300b6d20000dc460300b6d20000",
    "32ff22000c00000000000100000000000000000000000084d71700000000",
]

# --- DX profile: values plugged in from ArtemisSDR, NOT hardware-verified ----
# magic 0x32; RX+TX are BIDIRECTIONAL on a single port (50002) unlike the PRO's
# 50002/50003 split; 312500 Hz native rate; 92.5 kHz DDC0 offset. All from the
# ArtemisSDR reference. This project has never run against a DX, so verified is
# False and the DX path should be treated as a starting point to validate, not
# a known-good configuration.
DX = RadioProfile(
    name="SunSDR2 DX",
    magic=0x32,
    control_port=50001,
    rx_stream_port=50002,
    tx_stream_port=50002,       # DX is bidirectional on one port (ArtemisSDR)
    wire_rate=312500.0,         # ArtemisSDR (unverified here)
    ddc0_offset_hz=92500,       # ArtemisSDR SunSDRSetFreq (unverified here)
    discovery_family=0x32,
    init_sequence=_DX_INIT,     # from power_on_macro_dx[] (unverified here)
    rx_needs_tx_keepalive=True, # assumed like PRO; unverified on DX
    verified=False,
)

_PROFILES = {"PRO": PRO, "DX": DX}


def get_profile(variant: str) -> RadioProfile:
    v = variant.upper()
    if v not in _PROFILES:
        raise ValueError(f"unknown variant {variant!r}; known: {list(_PROFILES)}")
    return _PROFILES[v]
