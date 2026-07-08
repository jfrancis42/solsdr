"""
TX session orchestration — the full transmit chain, correctly sequenced.

Ties together: control-socket TX commands (PTT/drive/PA/config), the real-time
modulator (audio -> IQ), packetization (0xFD frames), and the TXPacer (precise
5.12 ms cadence) sending on the radio's TX stream port.

TX ENTRY/EXIT ORDERING is taken verbatim from the verified ExpertSDR3 wire
sequence (ArtemisSDR SunSDRSetPTT), which matters:
  ENTER (HF):  set currentPTT flag (stop 0xFE silence) -> reassert TX freq/mode
               (0x09/0x20) -> drive (0x17) -> MOX 0x06=1 -> open TX IQ gate ->
               PA 0x24=1 (only if PA on) -> pace 0xFD packets
  EXIT:        PA 0x24=0 (if it was on) -> MOX 0x06=0 -> config-block RX state
               -> resume RX (0xFE keepalive)

SAFETY: this class NEVER keys the radio unless armed via arm(confirm=True). The
`dest` for TX IQ defaults to the radio's TX port, but enter_tx() refuses to send
the 0x06 MOX command unless armed. For no-RF testing, construct with a loopback
dest and armed=False: the modulator+pacer+packetizer all run and can be captured,
but PTT is never asserted.
"""

import socket
import threading
from typing import Callable, Optional

import numpy as np

from .protocol import packet as pk
from .dsp.modulator import Modulator
from .dsp.tx_power import TXPowerCal, band_for_freq
from .protocol.tx_pacer import TXPacer


class TXSession:
    # Safety caps. max_drive bounds the raw drive byte no matter what watts or
    # raw value is requested — a hard ceiling for early testing. Raise it
    # deliberately once calibrated and confident.
    DEFAULT_MAX_DRIVE = 64          # ~6 W on the sqrt model @ 100 W full scale
    # Dead-man: auto-unkey if a keyed session runs longer than this without an
    # explicit exit_tx() or a kick_deadman() from the caller. Prevents a stuck
    # carrier. Default must clear legitimate long digital overs: WSPR/FST4W-120
    # transmit ~110-120 s, so a single over can approach 2 minutes. 300 s (5 min)
    # covers those plus SSB ragchew overs while still bounding a runaway. For a
    # continuous-keying mode, the caller should call kick_deadman() each block
    # (RealtimeTX-style feeders do) rather than rely on a huge timeout.
    DEFAULT_DEADMAN_S = 300.0
    # Drive byte used as a hard floor when an amp limit is set but the band is
    # uncalibrated (belt-and-suspenders behind tx_permitted()'s refusal).
    UNCAL_SAFE_DRIVE = 8

    def __init__(self, radio, mode='USB', audio_rate=48000, realtime=True,
                 power_cal: Optional[TXPowerCal] = None, verbose=True,
                 loopback_dest=None, max_drive=DEFAULT_MAX_DRIVE,
                 deadman_s=DEFAULT_DEADMAN_S, max_power_watts=None):
        """
        radio: an opened Radio (provides .ctrl, .radio_ip, .profile, .wire_rate).
        loopback_dest: (ip, port) to send TX IQ to instead of the radio's TX
            port — for no-RF validation. If None, uses the radio TX port.
        max_drive: hard ceiling on the drive byte (safety).
        deadman_s: auto-unkey after this many seconds keyed (0 disables).
        max_power_watts: hard OUTPUT-POWER ceiling in watts (e.g. 5.0 to protect
            a downstream amp rated for 5 W input). Set ONLY here (from CLI or
            config); there is deliberately no runtime setter, so neither the
            interactive shell nor a Hamlib client can raise it. Every power
            request (watts or raw drive) is clamped to it. None = no watts cap
            (only max_drive applies).
        """
        self.radio = radio
        self.profile = radio.profile
        self.wire_rate = radio.wire_rate
        self.mode = mode.upper()
        self.audio_rate = audio_rate
        self.realtime = realtime
        self.verbose = verbose
        self.power_cal = power_cal or TXPowerCal()
        self.max_drive = int(max_drive)
        self.deadman_s = float(deadman_s)
        self._deadman_timer: Optional[threading.Timer] = None
        # Amp-protection output-power ceiling (watts). Stored read-only: kept in
        # a name-mangled attribute and exposed only via the max_power_watts
        # property (no setter) so it cannot be mutated at runtime.
        self.__max_power_watts = (float(max_power_watts)
                                  if max_power_watts is not None else None)

        self.tx_ip = radio.radio_ip
        self.tx_port = self.profile.tx_stream_port
        self.loopback_dest = loopback_dest

        self.mod = Modulator(audio_rate=audio_rate, wire_rate=self.wire_rate,
                             mode=self.mode)
        # Gain applied to RAW-IQ TX input (iq_input=True path) before clipping.
        # The audio path is leveled inside the Modulator; the raw-IQ path is not,
        # so the caller controls amplitude here (1.0 = pass-through, expecting
        # samples already in [-1, 1]).
        self.tx_iq_gain = 1.0
        self._iq_buf = np.zeros(0, dtype=np.complex64)
        self._buf_lock = threading.Lock()
        self._seq = 0
        self._silence = pk.encode_iq_packet(
            np.zeros(pk.IQ_SAMPLES_PER_PKT, np.complex64), 0, self.profile.magic)

        self._sock: Optional[socket.socket] = None
        self._pacer: Optional[TXPacer] = None
        self._feeder: Optional[threading.Thread] = None
        self._running = False
        self._audio_iter = None

        self._armed = False
        self.keyed = False
        self.current_drive = 0
        self.pa_enabled = False

    def _log(self, *a):
        if self.verbose:
            from .log import log_line; log_line('tx-session', ' '.join(str(x) for x in a))

    @property
    def max_power_watts(self):
        """The amp-protection output ceiling in watts (read-only; None if unset)."""
        return self.__max_power_watts

    def _power_cap_drive(self) -> int:
        """Drive-byte ceiling implied by max_power_watts on the current band.

        The watts->drive mapping is only trustworthy on a CALIBRATED band. On
        an uncalibrated band the sqrt model can under-estimate the drive needed
        for N watts, i.e. the true output could EXCEED the limit and damage a
        downstream amp. So when a watts ceiling is set but the band is not
        calibrated, we clamp to a hard-conservative floor (and enter_tx refuses
        to key — see tx_permitted()). Returns 255 if no watts cap is set.
        """
        if self.__max_power_watts is None:
            return 255
        freq = self.radio.current_freq or 0
        band = band_for_freq(freq)
        if not self.power_cal.is_calibrated(band):
            # Not trustworthy — return a very low floor so that even if
            # something bypasses tx_permitted(), drive stays minimal.
            return self.UNCAL_SAFE_DRIVE
        drive, _ = self.power_cal.watts_to_drive(self.__max_power_watts, freq)
        return drive

    def tx_permitted(self) -> tuple:
        """Whether keying is allowed right now. Returns (ok, reason).

        Refuses if a max_power_watts amp-protection limit is set but the
        current band is not calibrated — because the watts ceiling cannot be
        honored without a measured curve. The calibration tool bypasses this by
        constructing with max_power_watts=None and doing its own bounded raw
        sweep.
        """
        if self.__max_power_watts is None:
            return True, 'no amp limit set'
        freq = self.radio.current_freq or 0
        band = band_for_freq(freq)
        if not self.power_cal.is_calibrated(band):
            return (False,
                    f'amp limit {self.__max_power_watts}W set but {band} is '
                    f'UNCALIBRATED — calibrate this band before TX to guarantee '
                    f'the limit')
        return True, f'{band} calibrated'

    # -- safety arming -----------------------------------------------------
    def arm(self, confirm: bool = False):
        """Enable actual keying. Must be called with confirm=True before
        enter_tx() will assert PTT. Without arming, the chain runs (modulate +
        pace + send to dest) but never keys the radio — safe for no-RF tests."""
        self._armed = bool(confirm)
        self._log(f'ARMED for transmit' if self._armed else 'disarmed')
        return self._armed

    def disarm(self):
        self._armed = False

    # -- power -------------------------------------------------------------
    def _cap(self, drive: int) -> int:
        """Apply BOTH safety ceilings, logging if either clamps:
          - max_drive: raw drive-byte ceiling
          - max_power_watts: amp-protection output ceiling (via cal table)
        The lower of the two wins. Neither is runtime-settable."""
        ceiling = min(self.max_drive, self._power_cap_drive())
        if drive > ceiling:
            reason = ('max_power %.1fW' % self.__max_power_watts
                      if self._power_cap_drive() <= self.max_drive
                      else 'max_drive %d' % self.max_drive)
            self._log(f'drive {drive} capped to {ceiling} ({reason})')
            return ceiling
        return max(0, drive)

    def set_power_watts(self, watts: float) -> dict:
        """Resolve target watts -> drive byte via the per-band cal table and
        set it on the radio (subject to the max_drive cap)."""
        freq = self.radio.current_freq or 0
        drive, calibrated = self.power_cal.watts_to_drive(watts, freq)
        capped = self._cap(drive)
        self.current_drive = capped
        if self.radio.ctrl:
            self.radio.ctrl.set_drive(capped)
        info = {'watts': watts, 'drive_byte': capped, 'requested_byte': drive,
                'capped': capped != drive, 'calibrated': calibrated,
                'freq_hz': freq}
        self._log(f'power {watts}W -> drive {capped} '
                  f'({"calibrated" if calibrated else "SQRT ESTIMATE — verify with wattmeter"})')
        return info

    def set_drive_raw(self, raw_byte: int):
        """Set the raw drive byte directly (0-255), subject to max_drive cap."""
        self.current_drive = self._cap(min(255, int(raw_byte)))
        if self.radio.ctrl:
            self.radio.ctrl.set_drive(self.current_drive)
        self._log(f'raw drive byte {self.current_drive}')

    # -- IQ feed -----------------------------------------------------------
    def _packet_source(self):
        with self._buf_lock:
            if len(self._iq_buf) < pk.IQ_SAMPLES_PER_PKT:
                return None
            chunk = self._iq_buf[:pk.IQ_SAMPLES_PER_PKT]
            self._iq_buf = self._iq_buf[pk.IQ_SAMPLES_PER_PKT:]
        pktb = pk.encode_iq_packet(chunk, self._seq, self.profile.magic)
        self._seq = (self._seq + 1) & 0xFFFF
        return pktb

    def _prep_iq(self, block) -> np.ndarray:
        """Condition one block of RAW complex IQ (already at wire_rate) for the
        TX buffer: apply tx_iq_gain and clip to the 24-bit-safe unit range so
        packing never wraps. No modulation, no resampling — the caller is
        responsible for delivering complex samples at the radio wire rate."""
        iq = np.asarray(block, dtype=np.complex64)
        if len(iq) == 0:
            return iq
        if self.tx_iq_gain != 1.0:
            iq = iq * self.tx_iq_gain
        peak = float(np.max(np.abs(iq)))
        if peak > 0.98:
            iq = iq * (0.98 / peak)
        return iq.astype(np.complex64)

    def _feed_loop(self, source_iter, iq_input=False):
        import time
        target_ahead = int(self.wire_rate * 0.8)
        for block in source_iter:
            if not self._running:
                break
            iq = self._prep_iq(block) if iq_input else self.mod.process(block)
            with self._buf_lock:
                self._iq_buf = np.concatenate([self._iq_buf, iq])
            # Real signal is still flowing -> refresh the dead-man. This keeps a
            # legitimate long over (WSPR, ragchew, big file) alive indefinitely
            # while a genuinely stalled source still trips the timeout.
            self.kick_deadman()
            while self._running:
                with self._buf_lock:
                    if len(self._iq_buf) < target_ahead:
                        break
                time.sleep(0.02)

    # -- session -----------------------------------------------------------
    def enter_tx(self, source_iter, watts: Optional[float] = None,
                 raw_drive: Optional[int] = None, pa: bool = False,
                 prebuffer_s: float = 0.5, iq_input: bool = False):
        """Begin a keyed TX session sourced from source_iter.

        source_iter: by default yields real-audio blocks (fed through the
            modulator). If iq_input=True it yields COMPLEX baseband IQ blocks
            already at the radio wire_rate (self.wire_rate), which are sent
            verbatim (gain + clip only, no modulation/resample) — this is the
            raw-IQ TX path for GNU Radio and custom waveform generators. The
            radio's TX mode/config-block is still set; on the SunSDR2 the
            0xFD IQ stream is the modulator's baseband input regardless.
        watts/raw_drive: set power before keying (watts via cal table, or raw).
        pa: enable the internal PA (0x24=1) — only if you have a PA and mean it.
        prebuffer_s: how much IQ to accumulate before keying. 0.5 s is safe for
            file/tone playback; for LIVE audio (a soundcard bridge) use a small
            value (e.g. 0.05) so the app-to-RF latency is low and the modulator
            isn't primed with a half-second of leading silence (which
            SSB-modulates to zero output — no RF at the start of the over).

        If not armed, does the whole chain to `dest` (loopback if configured)
        but does NOT send 0x06 / 0x24 — no RF.
        """
        import time
        ctrl = self.radio.ctrl
        self._seq = 0
        # Fresh modulator state each audio session (unused on the raw-IQ path).
        if not iq_input:
            self.mod = Modulator(audio_rate=self.audio_rate,
                                 wire_rate=self.wire_rate, mode=self.mode)

        # power first (state only; safe unkeyed)
        if watts is not None:
            self.set_power_watts(watts)
        elif raw_drive is not None:
            self.set_drive_raw(raw_drive)

        dest = self.loopback_dest or (self.tx_ip, self.tx_port)
        # For real TX, send from the Radio's RX socket (bound to 50002) so the
        # 0xFD packets go out on the same port/source as the 0xFE keepalives —
        # this is how ExpertSDR3 does it (0xFD replaces 0xFE on 50002 while
        # keyed). Only use a private socket for loopback (no-radio) testing.
        if self.loopback_dest is None and getattr(self.radio, 'rx_sock', None):
            self._sock = self.radio.rx_sock
            self._own_sock = False
        else:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._own_sock = True
        self._running = True

        # pre-buffer IQ before we key / pace
        self._feeder = threading.Thread(target=self._feed_loop,
                                        args=(source_iter,),
                                        kwargs={'iq_input': iq_input},
                                        daemon=True)
        self._feeder.start()
        need = int(self.wire_rate * prebuffer_s)
        deadline = time.time() + 3.0
        while time.time() < deadline:
            with self._buf_lock:
                if len(self._iq_buf) >= need:
                    break
            time.sleep(0.02)

        # === TX ENTRY SEQUENCE (verified ordering) ===
        # Safety gate: refuse to key if an amp-protection limit is set but the
        # band isn't calibrated (the watts ceiling can't be honored otherwise).
        permitted, reason = self.tx_permitted()
        if self._armed and ctrl and not permitted:
            self._log(f'REFUSING TO KEY: {reason}')
            self._armed = False  # force the no-RF path below
        if self._armed and ctrl:
            # 1. stop RX silence keepalive by marking PTT (radio.py checks this)
            self.radio._tx_active = True
            # 2. reassert TX freq (HF)
            if self.radio.current_freq:
                ctrl.set_frequency(self.radio.current_freq)
            # 3. TX CONFIG_BLOCK (0x20 byte18=0) — routes IQ through the
            #    modulator to the PA. REQUIRED: without it the radio keys but
            #    produces only carrier bias, no modulated output. Verified
            #    against both the ExpertSDR3 capture and ArtemisSDR's
            #    sunsdr_send_config_block_state(0) at TX entry.
            ctrl.set_config_block(tx=True)
            # 4. drive (0x17) — after the config block, before 0x06 (EESDR order)
            ctrl.set_drive(self.current_drive)
            # 5. key: MOX 0x06 = 1
            ctrl.set_ptt(True)
            self.keyed = True
            # 5. PA enable only if requested
            if pa:
                ctrl.set_pa(True)
                self.pa_enabled = True
            # 6. arm dead-man: auto-unkey if we're still keyed after deadman_s.
            if self.deadman_s > 0:
                self._deadman_timer = threading.Timer(self.deadman_s,
                                                      self._deadman_fire)
                self._deadman_timer.daemon = True
                self._deadman_timer.start()
            self._log(f'*** KEYED (MOX on){" dead-man %.0fs" % self.deadman_s if self.deadman_s else ""} ***')
        else:
            self._log('not armed — running chain to dest WITHOUT keying (no RF)')

        # 6. pace 0xFD IQ packets
        interval = pk.IQ_SAMPLES_PER_PKT / self.wire_rate
        self._pacer = TXPacer(interval, self._packet_source,
                              lambda b: self._sock.sendto(b, dest),
                              underrun_packet=self._silence,
                              realtime=self.realtime, verbose=self.verbose)
        self._pacer.start()
        self._log(f'TX IQ paced to {dest} @ {interval*1000:.3f} ms, mode {self.mode}')

    def _deadman_fire(self):
        """Dead-man expiry: force an unkey. Prevents a stuck carrier if the
        caller never calls exit_tx() (crash, hang, forgotten key-down)."""
        if self.keyed:
            self._log(f'!!! DEAD-MAN TIMEOUT ({self.deadman_s}s) — force unkey !!!')
            self.exit_tx()

    def kick_deadman(self):
        """Restart the dead-man countdown — call periodically during a long
        legitimate transmission (e.g. per audio block) to keep TX alive."""
        if self.keyed and self.deadman_s > 0:
            if self._deadman_timer:
                self._deadman_timer.cancel()
            self._deadman_timer = threading.Timer(self.deadman_s, self._deadman_fire)
            self._deadman_timer.daemon = True
            self._deadman_timer.start()

    def exit_tx(self):
        """End the keyed session with the verified exit ordering. Idempotent."""
        ctrl = self.radio.ctrl
        if self._deadman_timer:
            self._deadman_timer.cancel()
            self._deadman_timer = None
        # stop pacing first so no 0xFD packets go out after unkey
        if self._pacer:
            self._pacer.stop()
            self._pacer = None
        self._running = False
        if self._feeder:
            self._feeder.join(timeout=1)

        if self.keyed and ctrl:
            # === TX EXIT SEQUENCE (verified ordering) ===
            if self.pa_enabled:
                ctrl.set_pa(False)      # 0x24 = 0 only if we turned it on
                self.pa_enabled = False
            ctrl.set_ptt(False)         # 0x06 = 0
            ctrl.set_config_block(tx=False)  # restore RX config block (0x20 byte18=1)
            self.keyed = False
            self.radio._tx_active = False
            self._log('*** UNKEYED (MOX off), RX resumed ***')

        # Only close a socket we created; never close the Radio's shared rx_sock.
        if self._sock and getattr(self, '_own_sock', True):
            self._sock.close()
        self._sock = None

    def jitter(self):
        return self._pacer.gap_stats_ms() if self._pacer else None
