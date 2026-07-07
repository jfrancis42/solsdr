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

        self._tx = None            # active TXSession while keyed
        self._tx_lock = threading.Lock()
        self._keyed = False
        self._running = False

    def _log(self, msg):
        if self.verbose:
            print(f'[bridge] {msg}')

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
            self.monitor.play(first)
            yield first

        while self._keyed:
            chunk = self.devices.read_tx(block)
            if chunk is None:
                self.monitor.play(silence)
                yield silence
                time.sleep(0.005)
            else:
                self.monitor.play(chunk)
                yield chunk

    def set_ptt(self, on: bool):
        """PTT edge from the Hamlib client. Key/unkey a TXSession around the
        app's transmit period. Idempotent per edge."""
        with self._tx_lock:
            if on and not self._keyed:
                self._key()
            elif not on and self._keyed:
                self._unkey()

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
    def start(self):
        self.devices.start()
        self.monitor.start()
        # PTT is delivered by the rigctld poller calling bridge.set_ptt().
        # Keep the demod's sideband synced to the radio's mode on the fly (the
        # poller calls radio.set_mode when the app changes mode).
        self._install_mode_hook()
        self.radio.start_stream(self._on_iq)
        self._running = True
        self._log('audio bridge running')

    def _install_mode_hook(self):
        """Wrap radio.set_mode so the demod follows mode changes made via
        Hamlib (so USB/LSB sideband and CW/AM/FM demod track the app)."""
        orig = self.radio.set_mode

        def wrapped(mode):
            ok = orig(mode)
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
