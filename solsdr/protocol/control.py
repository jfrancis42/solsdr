"""SunSDR2 Control Protocol - UDP control socket communication.

Verified against a live SunSDR2 PRO on 2026-07-06 (receiving real FT8). Key
facts learned the hard way and encoded here:

  * The control socket MUST bind source port 50001. The radio ignores control
    traffic from any other source port even though discovery still works.
  * Power-on uses the verified PRO init sequence (protocol/poweron_pro.py),
    captured from ExpertSDR3 on a real PRO — NOT ArtemisSDR's extrapolated
    macro (their authors had only a DX).
  * Tuning: send 0x09 (primary) then 0x08 (companion). For the PRO both are the
    display frequency (DDC0 offset 0; the 92.5 kHz in ArtemisSDR is DX-only).
"""

import socket
import struct
import time
from typing import Optional

from .opcodes import *
from .poweron_pro import PRO_KEEPALIVE
from .profiles import get_profile, PRO_RATE_INDEX, PRO_RATE_INDEX_OFFSETS


class SolSDRControl:
    """Control interface for SunSDR2 Pro/DX radio."""

    def __init__(self, radio_ip: str, variant: str = 'PRO', local_ip: str = '',
                 sample_rate: float = 39062.5):
        self.radio_ip = radio_ip
        self.variant = variant.upper()
        self.profile = get_profile(self.variant)
        self.control_port = self.profile.control_port
        self.magic = self.profile.magic
        self.iq_rx_port = self.profile.rx_stream_port
        self.iq_tx_port = self.profile.tx_stream_port
        # DDC0 offset between PRIMARY (0x09) and COMPANION (0x08). Per-variant.
        self.DDC0_OFFSET_HZ = self.profile.ddc0_offset_hz
        # Requested IQ sample rate; maps to the rate index patched into the
        # STATE_SYNC packet. Only the PRO rate table is verified.
        self.sample_rate = float(sample_rate)

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # Bind source port 50001 (required — see module docstring).
        self.local_ip = local_ip
        self.sock.bind((local_ip, self.control_port))
        self.sock.settimeout(0.4)
        self.powered_on = False
        self.current_freq: Optional[int] = None
        self.current_mode = 'USB'
        # TX state
        self.ptt = False
        self.current_drive = 0
        self.pa_enabled = False
        # Reference clock: PRO boots with external 10 MHz (GPSDO) enabled.
        self.ext_reference = True
        # Front-end switches
        self.hf_lpf = False
        self.vhf_lna = False
        self.mic_source = 0  # 0=Mic1, 1=Mic2, 2=PC
        # RX2 (second receiver): STATE_SYNC byte 54 (0x02 on / 0x01 off).
        # Applied at power_on; flipping live requires a re-init.
        self.rx2_enabled = False

    # -- raw send helpers --------------------------------------------------
    def _send_hex(self, hexstr: str, expect_response: bool = True) -> Optional[bytes]:
        """Send a complete pre-built wire packet (hex string)."""
        self.sock.sendto(bytes.fromhex(hexstr), (self.radio_ip, self.control_port))
        if expect_response:
            try:
                data, _ = self.sock.recvfrom(1024)
                return data
            except socket.timeout:
                return None
        return None

    def _send_cmd(self, opcode: int, payload: bytes = b'',
                  expect_response: bool = True) -> Optional[bytes]:
        """Build an 18-byte-header control packet and send it."""
        header = bytearray(18)
        header[0] = self.magic
        header[1] = 0xFF
        header[2] = opcode
        header[3] = 0x00
        header[4:6] = struct.pack('<H', len(payload))
        header[10] = 0x01
        self.sock.sendto(bytes(header) + payload, (self.radio_ip, self.control_port))
        if expect_response:
            try:
                data, _ = self.sock.recvfrom(1024)
                return data
            except socket.timeout:
                return None
        return None

    # -- power / keepalive -------------------------------------------------
    def _patch_rate(self, pkt_hex: str) -> str:
        """If pkt is the STATE_SYNC (0x01) packet, patch its PRO‑only fields:
          * rate index at byte offsets 56 & 58 (uint16 LE) -> self.sample_rate
          * receiver count at byte 54 -> 0x02 if RX2 enabled else 0x01
        Other packets pass through unchanged. All offsets verified on the PRO.
        """
        if self.variant != 'PRO':
            return pkt_hex
        b = bytearray.fromhex(pkt_hex)
        if len(b) < 60 or b[2] != 0x01:
            return pkt_hex
        # rate index (only if non-default)
        if self.sample_rate != 39062.5:
            idx = PRO_RATE_INDEX.get(self.sample_rate)
            if idx is not None:
                for off in PRO_RATE_INDEX_OFFSETS:
                    b[off] = idx & 0xFF
                    b[off + 1] = (idx >> 8) & 0xFF
        # RX2: byte 54 = 0x02 (two receivers) / 0x01 (one)
        if self.rx2_enabled:
            b[54] = 0x02
        return b.hex()

    def power_on(self, verbose: bool = True) -> bool:
        """Bring the radio up using the verified PRO init sequence.

        Assumes discovery/wake has already occurred (use solsdr.wake.wake()
        first if the radio may be idle). Returns True if the radio responded to
        essentially all init packets.
        """
        init_seq = self.profile.init_sequence
        if not init_seq:
            print(f'✗ No init sequence for {self.profile.name} — must be '
                  f'captured from real hardware first.')
            return False
        # Patch the requested sample rate into the STATE_SYNC packet's rate-index
        # field (PRO only — verified table). Non-default rate on DX is not known.
        init_seq = [self._patch_rate(pkt) for pkt in init_seq]
        if verbose:
            print(f'Powering on radio ({self.profile.name} init sequence, '
                  f'{self.sample_rate:.1f} Hz)...')
        responses = sum(1 for pkt in init_seq if self._send_hex(pkt) is not None)
        total = len(init_seq)
        self.powered_on = responses >= total - 1  # allow one dropped ACK
        if verbose:
            mark = '✓' if self.powered_on else '✗'
            print(f'{mark} Power-on: {responses}/{total} responses')
        return self.powered_on

    def keepalive(self) -> bool:
        """Send a control keepalive (0x18). Returns True if acked."""
        return self._send_hex(PRO_KEEPALIVE) is not None

    # -- frequency ---------------------------------------------------------
    def _send_freq_pkt(self, opcode: int, sub: int, freq_hz: int) -> Optional[bytes]:
        """Send a frequency packet (0x09 primary or 0x08 companion).

        Wire value = freq_hz * 10 as u64 LE. sub-index (bytes 6-7): RX1=0, RX2=1.
        """
        header = bytearray(18)
        header[0] = self.magic
        header[1] = 0xFF
        header[2] = opcode
        header[3] = 0x00
        header[4:6] = struct.pack('<H', 8)
        header[6:8] = struct.pack('<H', sub)
        header[10] = 0x01
        self.sock.sendto(bytes(header) + struct.pack('<Q', freq_hz * 10),
                         (self.radio_ip, self.control_port))
        try:
            data, _ = self.sock.recvfrom(1024)
            return data
        except socket.timeout:
            return None

    def set_frequency(self, freq_hz: int) -> bool:
        """Tune RX1 to freq_hz. PRIMARY (0x09) then COMPANION (0x08)."""
        is_bcast_fm = 80_000_000 <= freq_hz <= 108_000_000
        offset = 0 if is_bcast_fm else self.DDC0_OFFSET_HZ
        r1 = self._send_freq_pkt(OP_FREQ_PRIMARY, 0, freq_hz - offset)
        r2 = self._send_freq_pkt(OP_FREQ_COMP, 0, freq_hz)
        if r1 is not None or r2 is not None:
            self.current_freq = freq_hz
            return True
        return False

    def set_mode(self, mode: str) -> bool:
        """Record the demod mode. Mode is applied client-side in the DSP; the
        radio streams wideband IQ regardless. Kept for API symmetry so the
        control/Hamlib layers can report and set mode."""
        self.current_mode = mode.upper()
        return True

    # -- TX control primitives --------------------------------------------
    # All three below are u32-value commands: 18-byte header (length field=4)
    # + 4-byte little-endian value at offset 18. Matches ArtemisSDR
    # sunsdr_send_u32_cmd(). These set state on the radio but do NOT by
    # themselves start the IQ stream — orchestration lives in Radio.
    def _send_u32(self, opcode: int, value: int) -> Optional[bytes]:
        header = bytearray(18)
        header[0] = self.magic
        header[1] = 0xFF
        header[2] = opcode
        header[3] = 0x00
        header[4:6] = struct.pack('<H', 4)
        header[10] = 0x01
        self.sock.sendto(bytes(header) + struct.pack('<I', value & 0xFFFFFFFF),
                         (self.radio_ip, self.control_port))
        try:
            data, _ = self.sock.recvfrom(1024)
            return data
        except socket.timeout:
            return None

    def set_ptt(self, on: bool) -> bool:
        """Key/unkey the radio: 0x06 MOX_PTT, 1=TX 0=RX.

        This is the raw wire command only. Correct TX entry/exit ORDERING
        (drive/config/PA relative to PTT, and stopping the 0xFE silence
        keepalive) is handled by Radio.enter_tx()/exit_tx(); calling this
        directly out of sequence can key the radio without valid IQ.
        """
        r = self._send_u32(OP_MOX_PTT, 1 if on else 0)
        self.ptt = bool(on)
        return r is not None

    def set_drive(self, raw_byte: int) -> bool:
        """Set TX drive level: 0x17, a raw 0-255 byte (voltage-domain, sqrt of
        power). This is NOT calibrated watts — see dsp/tx_power.py for the
        per-band watts->byte table. The radio applies its own PA curve."""
        raw_byte = max(0, min(255, int(raw_byte)))
        r = self._send_u32(OP_DRIVE, raw_byte)
        self.current_drive = raw_byte
        return r is not None

    def set_pa(self, enabled: bool) -> bool:
        """Enable/disable the internal PA: 0x24 PA_ENABLE.

        Per ArtemisSDR: only send PA_ENABLE=1 when actually turning the PA on;
        writing 0x24=0 spuriously can kill TX output. Radio powers on in
        PA-off state, so leave it alone unless intentionally enabling.
        """
        r = self._send_u32(OP_PA_ENABLE, 1 if enabled else 0)
        self.pa_enabled = bool(enabled)
        return r is not None

    def set_reference(self, external: bool) -> bool:
        """Select the frequency reference: 0x1D, 1=external 10 MHz (GPSDO),
        0=internal. Verified 2026-07-07 by capturing ExpertSDR3's Ext.Ref
        button toggling (payload cycled 00/01 exactly with the on/off states).
        The PRO powers on with external reference enabled (the init sequence
        sends 0x1D=1)."""
        r = self._send_u32(OP_EXT_REF, 1 if external else 0)
        self.ext_reference = bool(external)
        return r is not None

    def set_hf_lpf(self, engaged: bool) -> bool:
        """HF low-pass filter: 0x1B, 1=LPF engaged, 0=auto. Verified 2026-07-07
        against ExpertSDR3's HF.LPF button (payload cycled 01/00). NOTE: this is
        the same opcode ArtemisSDR labels RX2_ENABLE; on the PRO it drives the
        HF LPF (see also set_reference / the ARTEMISSDR notes)."""
        r = self._send_u32(OP_HF_LPF, 1 if engaged else 0)
        self.hf_lpf = bool(engaged)
        return r is not None

    def set_vhf_lna(self, on: bool) -> bool:
        """VHF low-noise amplifier: 0x05 with byte-18 0x82=on, 0x02=off.
        Verified 2026-07-07 against ExpertSDR3's VHF.LNA button (relay click +
        payload 82/02). 0x05 also carries preamp/att state (0x80-0x83); the LNA
        uses the 0x82/0x02 pair specifically."""
        r = self._send_u32(OP_VHF_LNA, 0x82 if on else 0x02)
        self.vhf_lna = bool(on)
        return r is not None

    # Mic source values (0x21 byte18), ALL verified against ExpertSDR3 on the
    # PRO 2026-07-07. The radio only has TWO values: 0=Mic1, 1=Mic2. The GUI's
    # "PC" option sends the SAME 0x21=1 as Mic2 (Mic2 and PC are indistinguishable
    # at the radio; the PC-vs-Mic2 choice is handled in software audio routing,
    # not on the wire). 'pc' is accepted as an alias for 1 for convenience.
    MIC_SOURCES = {'mic1': 0, 'mic2': 1, 'pc': 1}

    def set_mic_source(self, source) -> bool:
        """Select the mic source: 0x21, byte18 0=Mic1, 1=Mic2. NOTE: the GUI's
        'PC' option sends the same value as Mic2 (verified) — the radio doesn't
        distinguish them, so 'pc' maps to 1 here too."""
        if isinstance(source, str):
            val = self.MIC_SOURCES.get(source.lower())
            if val is None:
                raise ValueError(f'mic source must be one of {list(self.MIC_SOURCES)} or int')
        else:
            val = int(source)
        r = self._send_u32(OP_MIC_SOURCE, val)
        self.mic_source = val
        return r is not None

    # CONFIG_BLOCK (0x20) templates captured from ExpertSDR3 on the PRO. The two
    # forms differ ONLY at byte 18: 0x00 = TX (routes IQ through the modulator to
    # the PA), 0x01 = RX. ExpertSDR3 sends the TX form right before 0x06 key-up
    # and the RX form on unkey. WITHOUT the TX config block, the radio keys but
    # never modulates (carrier bias only) — this was the "keys but no output" bug.
    _CONFIG_BLOCK_TX = "01ff20003400000000000100000000000000000000000100000001000000000000006400000000000000000000001e000000bc02000007000000640000002c01000064000000"
    _CONFIG_BLOCK_RX = "01ff20003400000000000100000000000000010000000100000001000000000000006400000000000000000000001e000000bc02000007000000640000002c01000064000000"

    def set_config_block(self, tx: bool) -> bool:
        """Send the CONFIG_BLOCK (0x20) in TX or RX form. tx=True routes IQ to
        the modulator/PA (required on TX entry); tx=False restores RX."""
        pkt = self._CONFIG_BLOCK_TX if tx else self._CONFIG_BLOCK_RX
        # magic byte may differ per variant; patch byte 0.
        b = bytearray.fromhex(pkt)
        b[0] = self.magic
        return self._send_hex(b.hex()) is not None

    def close(self):
        self.sock.close()
