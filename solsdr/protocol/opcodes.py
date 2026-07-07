"""
SunSDR2 Protocol Opcodes

Extracted from ArtemisSDR sunsdr.h
Reference: ~/Dropbox/build/ArtemisSDR/Project Files/Source/ChannelMaster/sunsdr.h
"""

# UDP Ports
CONTROL_PORT = 50001
STREAM_PORT = 50002

# Packet Magic
MAGIC_0_DX = 0x32
MAGIC_1 = 0xFF

# Control Opcodes (port 50001)
OP_STATE_SYNC = 0x01
OP_POWER_OFF = 0x02
OP_PREAMP_ATT = 0x05  # also carries VHF LNA on/off: byte18 0x82=LNA on, 0x02=off
OP_VHF_LNA = 0x05     # alias: ExpertSDR3 VHF.LNA button (relay-switched)
OP_MOX_PTT = 0x06
OP_INFO_QUERY = 0x07
OP_FREQ_COMP = 0x08  # DDC companion frequency
OP_FREQ_PRIMARY = 0x09  # Primary VFO
OP_STATE_REQ_A = 0x0E
OP_STATE_REQ_B = 0x10
OP_RX_ANT = 0x15
OP_DRIVE = 0x17  # TX power (sqrt-encoded)
OP_KEEPALIVE = 0x18
OP_EXT_REF = 0x1D      # External 10 MHz reference (GPSDO): u32 1=external, 0=internal
OP_QUERY_FIXED = 0x1A  # Firmware version query
OP_HF_LPF = 0x1B      # ExpertSDR3 HF.LPF button: u32 1=LPF engaged, 0=auto
                       # (was assumed RX2_ENABLE from the ArtemisSDR reference;
                       #  verified on PRO hardware to drive the HF low-pass filter)
OP_RX2_ENABLE = 0x1B   # kept as an alias; same opcode, meaning unverified on PRO
OP_ANT_PREAMBLE = 0x1E
OP_MIC_SOURCE = 0x21  # Mic source: byte18 0=Mic1, 1=Mic2 (verified PRO). The
                       # GUI "PC" option sends the SAME 1 as Mic2 (radio can't
                       # tell them apart; PC vs Mic2 is software audio routing).
OP_CONFIG_BLOCK = 0x20  # Mode selection
OP_STREAM_XPORT = 0x22
OP_PA_ENABLE = 0x24
OP_EXT_CTRL = 0x27
OP_STATE_REPEAT = 0x5A
OP_POWER_WAKE = 0x5F

# IQ Stream Opcodes (port 50002)
OP_IQ_RX_IDLE = 0xFE  # RX state / TX idle keepalive
OP_IQ_TX_ACTIVE = 0xFD  # TX active with voice IQ
OP_TELEMETRY = 0x1F   # periodic status on 50002: supply V/A + temperature

# Preamp/Attenuator States
PREAMP_ATT_M20 = 0x80  # -20 dB
PREAMP_ATT_M10 = 0x81  # -10 dB
PREAMP_ATT_0 = 0x82    # 0 dB (bypass)
PREAMP_ATT_P10 = 0x83  # +10 dB preamp

# Mode Codes (internal, used in CONFIG_BLOCK)
MODE_LSB = 0xBC
MODE_USB = 0xF5
MODE_AM = 0x28

# Packet Sizes
IQ_PKT_SIZE = 1210
IQ_HDR_SIZE = 10
IQ_PAYLOAD_SIZE = 1200
IQ_COMPLEX_PER_PKT = 200  # 200 complex samples per packet
IQ_BYTES_PER_SAMPLE = 6   # 24-bit Q + 24-bit I

# Control Packet Header Size
CTL_HDR_SIZE = 18

# Frequency Scaling
FREQ_SCALE = 10  # Wire value = Hz * 10

# DDC Offsets (Hz)
DDC0_OFFSET_HZ = 92500
DDC1_OFFSET_HZ = 22000
