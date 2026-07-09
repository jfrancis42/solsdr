"""
Audio bridge: SunSDR2 <-> virtual PulseAudio devices, for JS8Call / WSJT-X /
fldigi and friends.

Wiring:
  RX:  Radio.start_stream -> Demodulator -> PulseAudioDevices.write_rx
       (app reads <prefix>-rx.monitor / <prefix>-rx-mic)
  TX:  app writes <prefix>-tx  -> PulseAudioDevices.read_tx -> Modulator
       (inside TXSession) -> radio TX IQ
  Control: the app talks to a real rigctld (launched by RigctldPoller), which
       mirrors freq/mode to the radio; a PTT edge from the poller is routed to
       bridge.set_ptt so the bridge keys/unkeys a TXSession around the app's
       transmit period.

The app controls the radio entirely through Hamlib. This bridge only moves
audio and reacts to PTT — it does not tune or set mode itself (it mirrors the
radio's current mode into the demod/mod so the sideband matches).
"""
import threading
import time

import numpy as np

from ..dsp.demod import Demodulator
from .pulse_devices import PulseAudioDevices, SpeakerMonitor


class JS8AudioBridge:
    def __init__(self, radio, prefix='solsdr', audio_rate=48000,
                 tx_mode='USB', max_drive=255, max_power_watts=None,
                 tx_watts=None, monitor_sink=None, monitor_gain=0.7,
                 verbose=True):
        self.radio = radio
        self.audio_rate = int(audio_rate)
        self.verbose = verbose
        self.tx_mode = tx_mode.upper()
        self.max_drive = max_drive
        self.max_power_watts = max_power_watts
        # Requested TX output setpoint in watts. None -> use max_power_watts if
        # set, else full drive. Clamped to max_power_watts at key time.
        self.tx_watts = float(tx_watts) if tx_watts is not None else None

        self.devices = PulseAudioDevices(prefix, audio_rate, verbose=verbose)
        # Optional local speaker monitor for RX + exact TX audio.
        self.monitor = SpeakerMonitor(monitor_sink, audio_rate,
                                      gain=monitor_gain, verbose=verbose)
        # agc='off' -> linear: preserves the relative amplitudes of the ~50
        # simultaneous FT8 tones (AGC measurably lowers decode count).
        self.demod = Demodulator(wire_rate=radio.wire_rate,
                                 audio_rate=audio_rate, mode=tx_mode, agc='off')

        self.mic_gain = 1.0        # TX-audio gain multiplier, live-adjustable
        # CW send settings (shell `cw <text>`). char_wpm = element speed;
        # word_wpm = Farnsworth spacing speed (defaults to char_wpm = standard).
        self.cw_char_wpm = 20.0
        self.cw_word_wpm = None    # None => standard (word == char)
        self.cw_tone_hz = 600.0
        self._tx = None            # active TXSession while keyed
        self._tx_lock = threading.Lock()
        self._keyed = False
        self._running = False
        self._external_iq = False  # True => fed by feed_iq(), not own start_stream

    def _log(self, msg):
        if self.verbose:
            from ..log import log_line; log_line('bridge', msg)

    # -- RX path ----------------------------------------------------------
    def _on_iq(self, iq: np.ndarray):
        """RX IQ callback: demodulate and push to the RX sink. Suppressed
        while transmitting (radio streams no useful RX during TX)."""
        if self._keyed:
            return
        try:
            audio = self.demod.process(iq)
        except Exception as e:
            self._log(f'demod error: {e}')
            return
        if audio is not None and len(audio):
            self.devices.write_rx(audio)
            self.monitor.play(audio)  # hear RX on the speaker

    # -- TX path ----------------------------------------------------------
    def _tx_audio_iter(self):
        """Yield ~20 ms blocks of app audio for the modulator.

        First, BLOCK until real audio arrives from the app (up to ~2 s), so we
        don't prime the modulator with leading silence — SSB-modulated silence
        is zero output, i.e. no RF at the start of the over, and it also inflates
        the app-to-RF latency. Once audio is flowing, emit silence only on a
        momentary underrun so the 5.12 ms pacer never starves.

        Also plays each block to the speaker monitor — this is the EXACT audio
        fed to the modulator, so any underrun-silence or glitch the bridge
        introduces is audible, not just JS8Call's raw output."""
        block = self.audio_rate // 50  # 20 ms
        silence = np.zeros(block, dtype=np.float32)

        # Wait for the first real audio block (bounded, so a silent Tune or a
        # missing audio route can't hang the keyed session forever).
        waited = 0.0
        first = None
        while self._keyed and waited < 2.0:
            first = self.devices.read_tx(block)
            if first is not None:
                break
            time.sleep(0.005)
            waited += 0.005
        if first is not None:
            first = self._apply_mic_gain(first)
            self.monitor.play(first)
            yield first

        while self._keyed:
            chunk = self.devices.read_tx(block)
            if chunk is None:
                self.monitor.play(silence)
                yield silence
                time.sleep(0.005)
            else:
                chunk = self._apply_mic_gain(chunk)
                self.monitor.play(chunk)
                yield chunk

    def _apply_mic_gain(self, block):
        """Scale TX audio by the live mic-gain multiplier (1.0 = unity). Clip to
        [-1, 1] so a high gain can't overflow the modulator's input leveling.
        Read fresh each block so gain changes apply live during an over."""
        g = self.mic_gain
        if g == 1.0:
            return block
        return np.clip(block * g, -1.0, 1.0).astype(np.float32)

    def set_ptt(self, on: bool):
        """PTT edge from the Hamlib client. Key/unkey a TXSession around the
        app's transmit period. Idempotent per edge."""
        with self._tx_lock:
            if on and not self._keyed:
                self._key()
            elif not on and self._keyed:
                self._unkey()

    # -- live TX-setting control (used by the unified shell) ----------------
    def is_keyed(self):
        return self._keyed

    def set_tx_watts(self, watts):
        """Set the TX output setpoint. Applies to a live (keyed) transmission
        immediately via the active TXSession, and to subsequent overs. `watts`
        is clamped to max_power_watts by the TXSession regardless."""
        self.tx_watts = float(watts) if watts is not None else None
        with self._tx_lock:
            if self._tx is not None and self.tx_watts is not None:
                self._tx.set_power_watts(self.tx_watts)   # live drive change

    def set_mic_gain(self, g):
        """Set the TX-audio (mic) gain multiplier; takes effect on the next
        audio block, so it applies live during a transmission."""
        self.mic_gain = max(0.0, float(g))

    def set_prefix(self, prefix):
        """Rename the virtual audio devices to <prefix>-rx / <prefix>-tx, live.
        Recreates the PulseAudio sinks — any app bound to the old devices will
        drop and must be repointed. Refused while transmitting. Returns
        (ok, message)."""
        prefix = str(prefix).strip()
        if not prefix:
            return False, 'empty prefix'
        with self._tx_lock:
            if self._keyed:
                return False, 'transmitting — device rename refused'
        try:
            self.devices.set_prefix(prefix)
        except Exception as e:  # noqa: BLE001
            return False, f'rename failed: {e}'
        return True, (f'audio devices now {prefix}-rx.monitor (RX) / '
                      f'{prefix}-tx (TX) — repoint fldigi/WSJT-X/JS8Call')

    def tune_carrier(self, seconds=3.0, watts=None, tone_hz=1000.0):
        """Key a steady CW tuning carrier for `seconds`, then unkey.

        The ONE case where solsdr itself keys the transmitter (everything else
        is app/CAT-driven PTT): a deliberate, time-bounded tuning key-up. Uses
        the same interlocked TXSession as normal TX (arm, amp-limit clamp,
        calibration gate, dead-man). Refuses if a transmission is already in
        progress. `watts` defaults to the current TX setpoint (self.tx_watts).
        Returns (ok, message). Blocking for the duration.
        """
        import threading
        import numpy as np
        with self._tx_lock:
            if self._keyed:
                return False, 'already transmitting — tune refused'
            self._keyed = True
        try:
            from ..tx_session import TXSession
            w = self.tx_watts if watts is None else float(watts)
            mode = 'USB'                    # a tone in USB = a clean carrier offset
            stop = threading.Event()

            def tone_iter():
                block = self.audio_rate // 50
                ph = 0.0
                dphi = 2 * np.pi * tone_hz / self.audio_rate
                while not stop.is_set():
                    idx = np.arange(block)
                    blk = (0.9 * np.sin(ph + dphi * idx)).astype(np.float32)
                    ph = (ph + dphi * block) % (2 * np.pi)
                    yield self._apply_mic_gain(blk)

            tx = TXSession(self.radio, mode=mode, audio_rate=self.audio_rate,
                           max_drive=self.max_drive,
                           max_power_watts=self.max_power_watts,
                           deadman_s=max(seconds + 2, 5), verbose=self.verbose)
            permitted, reason = tx.tx_permitted()
            if not permitted:
                return False, f'tune refused: {reason}'
            tx.arm(confirm=True)
            self._tx = tx
            tx.enter_tx(tone_iter(), watts=w, pa=False, prebuffer_s=0.1)
            self._log(f'*** TUNE carrier: {seconds:g}s @ '
                      f'{("%.1f W" % w) if w is not None else "full drive"} ***')
            import time as _t
            _t.sleep(float(seconds))
            stop.set()
            tx.exit_tx()
            return True, (f'tune complete: {seconds:g}s @ '
                          f'{("%.1f W" % w) if w is not None else "full drive"}')
        finally:
            self._tx = None
            self._keyed = False

    def send_cw(self, text, watts=None):
        """Encode `text` to Morse (at the configured char/word WPM + tone) and
        transmit it, then unkey. Farnsworth timing honored (word_wpm < char_wpm).
        Uses the interlocked TXSession like tune/normal TX. Refuses if already
        keyed. Blocks until the message is sent. Returns (ok, message)."""
        import threading
        import numpy as np
        from ..dsp.cw_decode import CWEncoder
        text = text.strip()
        if not text:
            return False, 'nothing to send'
        with self._tx_lock:
            if self._keyed:
                return False, 'already transmitting — cw send refused'
            self._keyed = True
        try:
            enc = CWEncoder(sample_rate=self.audio_rate, pitch=self.cw_tone_hz,
                            char_wpm=self.cw_char_wpm, word_wpm=self.cw_word_wpm)
            audio = enc.encode(text).astype(np.float32)
            if len(audio) == 0:
                return False, 'no sendable characters in message'

            # One-shot iterator: yield the encoded audio in modulator-sized
            # blocks, then a little trailing silence, then stop (ends the over).
            block = self.audio_rate // 50
            def cw_iter():
                for i in range(0, len(audio), block):
                    yield self._apply_mic_gain(audio[i:i + block])
                for _ in range(3):          # ~60 ms tail so the last dit clears
                    yield np.zeros(block, dtype=np.float32)

            from ..tx_session import TXSession
            w = self.tx_watts if watts is None else float(watts)
            tx = TXSession(self.radio, mode='USB', audio_rate=self.audio_rate,
                           max_drive=self.max_drive,
                           max_power_watts=self.max_power_watts,
                           deadman_s=max(len(audio) / self.audio_rate + 5, 10),
                           verbose=self.verbose)
            permitted, reason = tx.tx_permitted()
            if not permitted:
                return False, f'cw send refused: {reason}'
            tx.arm(confirm=True)
            self._tx = tx
            dur = len(audio) / self.audio_rate
            eff = self.cw_word_wpm or self.cw_char_wpm
            self._log(f'*** CW TX: {len(text)} chars, ~{dur:.1f}s @ '
                      f'{self.cw_char_wpm:g}wpm'
                      f'{"/%gwpm Farnsworth" % self.cw_word_wpm if self.cw_word_wpm and self.cw_word_wpm < self.cw_char_wpm else ""} ***')
            tx.enter_tx(cw_iter(), watts=w, pa=False, prebuffer_s=0.1)
            # Wait for the whole message (plus a margin) to pace out, then unkey.
            import time as _t
            _t.sleep(dur + 0.5)
            tx.exit_tx()
            return True, (f'sent {len(text)} chars in ~{dur:.1f}s '
                          f'({self.cw_char_wpm:g}/{eff:g} wpm, {self.cw_tone_hz:g} Hz)')
        finally:
            self._tx = None
            self._keyed = False

    def set_cw(self, char_wpm=None, word_wpm=None, tone_hz=None):
        """Set CW send parameters. char_wpm = element speed; word_wpm =
        Farnsworth spacing speed (pass a value < char_wpm for Farnsworth, or
        None/>=char for standard); tone_hz = sidetone/pitch (default 600)."""
        if char_wpm is not None:
            self.cw_char_wpm = float(char_wpm)
        if word_wpm is not None:
            self.cw_word_wpm = None if word_wpm in ('', None) else float(word_wpm)
        if tone_hz is not None:
            self.cw_tone_hz = float(tone_hz)

    def _key(self):
        # Import here so RX-only use doesn't require the TX stack.
        from ..tx_session import TXSession
        mode = self.radio.current_mode or self.tx_mode
        self._keyed = True
        self.devices.flush_tx()  # drop pre-key audio
        tx = TXSession(self.radio, mode=mode, audio_rate=self.audio_rate,
                       realtime=True, max_drive=self.max_drive,
                       max_power_watts=self.max_power_watts,
                       verbose=self.verbose)
        tx.arm(confirm=True)
        # Power setpoint: use tx_watts if set, else the amp ceiling, else full
        # drive. The TXSession clamps to max_power_watts regardless.
        watts = self.tx_watts if self.tx_watts is not None \
            else self.max_power_watts
        # Small prebuffer: live audio, so we want low app-to-RF latency. The
        # iterator blocks for the first real audio, so the modulator's buffer
        # fills with signal (not silence) and we key promptly once audio flows.
        tx.enter_tx(self._tx_audio_iter(), watts=watts, pa=False,
                    prebuffer_s=0.05)
        self._tx = tx
        self._log(f'PTT ON — TX keyed ({mode}, '
                  f'{("%.1f W" % watts) if watts is not None else "full drive"})')

    def _unkey(self):
        self._keyed = False
        if self._tx is not None:
            try:
                self._tx.exit_tx()
            except Exception as e:
                self._log(f'exit_tx error: {e}')
            self._tx = None
        self._log('PTT OFF — TX unkeyed, RX resumed')

    # -- lifecycle --------------------------------------------------------
    def start(self, external_iq=False):
        """Start the bridge. If external_iq=True the caller feeds RX IQ via
        feed_iq() (used by the unified transceiver, where the receiver owns the
        single Radio.start_stream callback and fans IQ out to both consumers);
        otherwise the bridge owns start_stream itself (standalone `solsdr.audio`)."""
        self.devices.start()
        self.monitor.start()
        # PTT is delivered by the rigctld poller calling bridge.set_ptt().
        # Keep the demod's sideband synced to the radio's mode on the fly (the
        # poller calls radio.set_mode when the app changes mode).
        self._install_mode_hook()
        self._external_iq = external_iq
        if not external_iq:
            self.radio.start_stream(self._on_iq)
        self._running = True
        self._log(f'audio bridge running{" (external IQ feed)" if external_iq else ""}')

    def feed_iq(self, iq):
        """Feed one RX IQ block to the bridge (external_iq mode). Safe to call
        from the receiver's fan-out; the bridge ignores RX while keyed."""
        self._on_iq(iq)

    def _install_mode_hook(self):
        """Wrap radio.set_mode so the demod follows mode changes made via
        Hamlib (so USB/LSB sideband and CW/AM/FM demod track the app)."""
        orig = self.radio.set_mode

        def wrapped(mode, *args, **kwargs):
            # Pass through all args (e.g. rx=) so the receiver's per-channel
            # set_mode(mode, rx=N) still works with the hook installed. Only
            # follow the bridge demod for RX1 (rx==0 / default).
            ok = orig(mode, *args, **kwargs)
            rx = kwargs.get('rx', args[0] if args else 0)
            if rx == 0:
                try:
                    self.demod.set_mode(mode)
                except Exception:
                    pass
            return ok
        self.radio.set_mode = wrapped

    def stop(self):
        self._running = False
        with self._tx_lock:
            if self._keyed:
                self._unkey()
        self.monitor.stop()
        self.devices.stop()
        self._log('audio bridge stopped')
