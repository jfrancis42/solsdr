"""
Composable, STATEFUL audio/IQ DSP filters for the RX chain.

Adapted from the hamlib-audio-sidecar DSP suite, but reworked to carry filter
state across streaming blocks. The sidecar's originals re-designed/re-init the
filters on every call, which is fine for one-shot buffers but clicks at block
boundaries in a continuous stream. Each filter here holds its zi state so a
per-block streaming pipeline stays glitch-free.

Filters (all no-ops when their level/param is zero/off):
  * IQChannelFilter   — band-limit complex IQ to the channel before demod
  * NotchFilter       — kill a single interfering carrier (auto or manual)
  * AudioPeakFilter   — very narrow BP to pull CW/RTTY out of noise
  * NoiseBlanker      — impulse/click removal
  * NoiseReducer      — spectral-subtraction broadband NR
  * Squelch           — mute below an RMS threshold (with hysteresis)

Design goal: cheap enough for a Pi (see the SDR's Pi analysis) — IIR/FIR with
persistent state, block-rate FFT only for NR.
"""

import numpy as np
from scipy import signal


class IQChannelFilter:
    """Low-pass complex IQ to +/- bw/2 around 0 Hz (front-end selectivity).

    Applied to the baseband IQ BEFORE demod so out-of-channel energy (strong
    adjacent signals, noise) never reaches the detector. Stateful FIR.
    """
    def __init__(self, wire_rate, bandwidth_hz, ntaps=121):
        self.wire_rate = float(wire_rate)
        self.bandwidth_hz = float(bandwidth_hz)
        self._fir = None
        self._zi = None
        self._design()

    def _design(self):
        if self.bandwidth_hz >= self.wire_rate * 0.95:
            self._fir = None
            return
        cutoff = (self.bandwidth_hz / 2.0) / (self.wire_rate / 2.0)
        cutoff = min(max(cutoff, 1e-3), 0.999)
        self._fir = signal.firwin(121, cutoff).astype(np.float64)
        self._zi = np.zeros(len(self._fir) - 1, dtype=np.complex128)

    def set_bandwidth(self, bw_hz):
        self.bandwidth_hz = float(bw_hz)
        self._design()

    def process(self, iq):
        if self._fir is None:
            return iq
        out, self._zi = signal.lfilter(self._fir, 1.0,
                                       iq.astype(np.complex128), zi=self._zi)
        return out.astype(np.complex64)


class NotchFilter:
    """IIR notch to remove a single interfering tone. Stateful (lfilter zi)."""
    def __init__(self, sample_rate, notch_hz=0.0, bandwidth_hz=50.0):
        self.sample_rate = float(sample_rate)
        self.notch_hz = float(notch_hz)
        self.bandwidth_hz = float(bandwidth_hz)
        self._ba = None
        self._zi = None
        self._design()

    def _design(self):
        if self.notch_hz <= 0 or self.notch_hz >= self.sample_rate / 2.0:
            self._ba = None
            return
        Q = max(1.0, self.notch_hz / self.bandwidth_hz)
        # iirnotch: with fs given, the frequency argument is in Hz (do NOT
        # pre-normalize — that double-normalization made the notch a no-op).
        b, a = signal.iirnotch(self.notch_hz, Q, fs=self.sample_rate)
        self._ba = (b, a)
        self._zi = signal.lfilter_zi(b, a) * 0.0

    def set_notch(self, notch_hz, bandwidth_hz=None):
        self.notch_hz = float(notch_hz)
        if bandwidth_hz is not None:
            self.bandwidth_hz = float(bandwidth_hz)
        self._design()

    def process(self, audio):
        if self._ba is None:
            return audio
        b, a = self._ba
        out, self._zi = signal.lfilter(b, a, audio, zi=self._zi)
        return out.astype(np.float32)


class AudioPeakFilter:
    """Very narrow bandpass around a center (CW pitch / RTTY tone) to pull a
    weak tone out of noise. level 0..1 sets both narrowness and dry/wet mix.
    Stateful SOS."""
    def __init__(self, sample_rate, center_hz=600.0, level=0.0):
        self.sample_rate = float(sample_rate)
        self.center_hz = float(center_hz)
        self.level = float(level)
        self._sos = None
        self._zi = None
        self._design()

    def _design(self):
        if self.level <= 0.0:
            self._sos = None
            return
        bw = 200.0 - 150.0 * self.level     # 200 Hz @0 -> 50 Hz @1
        nyq = self.sample_rate / 2.0
        low = max((self.center_hz - bw / 2) / nyq, 1e-3)
        high = min((self.center_hz + bw / 2) / nyq, 0.999)
        if low >= high:
            self._sos = None
            return
        self._sos = signal.butter(4, [low, high], btype='band', output='sos')
        self._zi = signal.sosfilt_zi(self._sos)

    def set(self, level=None, center_hz=None):
        if level is not None:
            self.level = float(level)
        if center_hz is not None:
            self.center_hz = float(center_hz)
        self._design()

    def process(self, audio):
        if self._sos is None:
            return audio
        filt, self._zi = signal.sosfilt(self._sos, audio, zi=self._zi)
        return (audio * (1.0 - self.level) + filt * self.level).astype(np.float32)


class NoiseBlanker:
    """Impulse noise blanker: detect samples whose sample-to-sample jump greatly
    exceeds the running deviation and interpolate over them. level 0..1 sets
    sensitivity. Carries the last sample across blocks for the diff."""
    def __init__(self, level=0.0):
        self.level = float(level)
        self._prev = 0.0

    def process(self, audio):
        if self.level <= 0.0 or len(audio) == 0:
            if len(audio):
                self._prev = float(audio[-1])
            return audio
        diff = np.diff(audio, prepend=self._prev)
        self._prev = float(audio[-1])
        thr = np.std(diff) * (5.0 - 4.0 * self.level)
        if thr <= 0:
            return audio
        idx = np.where(np.abs(diff) > thr)[0]
        out = audio.copy()
        for i in idx:
            if 0 < i < len(out) - 1:
                out[i] = 0.5 * (out[i - 1] + out[i + 1])
        return out.astype(np.float32)


class NoiseReducer:
    """Spectral-subtraction broadband noise reduction. level 0..1. Estimates a
    noise floor from the quietest spectral bins over a rolling window so it
    adapts without needing a dedicated 'noise-only' segment."""
    def __init__(self, level=0.0):
        self.level = float(level)
        self._noise_mag = None

    def process(self, audio):
        if self.level <= 0.0 or len(audio) < 64:
            return audio
        S = np.fft.rfft(audio)
        mag = np.abs(S)
        phase = np.angle(S)
        # Rolling noise estimate: track a slow minimum-follower of the magnitude
        if self._noise_mag is None or len(self._noise_mag) != len(mag):
            self._noise_mag = mag.copy()
        else:
            # decay toward current mag; rise slowly, fall fast (min-tracking)
            self._noise_mag = np.minimum(mag, 0.95 * self._noise_mag + 0.05 * mag)
        reduced = np.maximum(mag - self.level * self._noise_mag, mag * 0.1)
        out = np.fft.irfft(reduced * np.exp(1j * phase), n=len(audio))
        return out.astype(np.float32)


class Squelch:
    """Mute audio below an RMS threshold, with hysteresis so it doesn't chatter
    on the threshold. level 0..1."""
    def __init__(self, level=0.0):
        self.level = float(level)
        self._open = False

    def process(self, audio):
        if self.level <= 0.0 or len(audio) == 0:
            return audio
        rms = float(np.sqrt(np.mean(audio ** 2)))
        open_thr = self.level * 0.1
        close_thr = open_thr * 0.7          # hysteresis
        if self._open:
            if rms < close_thr:
                self._open = False
        else:
            if rms > open_thr:
                self._open = True
        return audio if self._open else np.zeros_like(audio)


class RXFilterChain:
    """Ordered, stateful post-demod audio filter chain. All stages default off.

    Order: noise blanker -> notch -> noise reduction -> audio peak filter ->
    squelch. (Blank impulses first, kill carriers, then broadband NR, then
    narrow the passband, then gate.)
    """
    def __init__(self, sample_rate, cw_pitch=600.0):
        self.sample_rate = sample_rate
        self.nb = NoiseBlanker(0.0)
        self.notch = NotchFilter(sample_rate, 0.0)
        self.nr = NoiseReducer(0.0)
        self.apf = AudioPeakFilter(sample_rate, cw_pitch, 0.0)
        self.squelch = Squelch(0.0)

    def process(self, audio):
        audio = self.nb.process(audio)
        audio = self.notch.process(audio)
        audio = self.nr.process(audio)
        audio = self.apf.process(audio)
        audio = self.squelch.process(audio)
        return audio
