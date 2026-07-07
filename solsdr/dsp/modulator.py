"""
SSB / AM / FM modulator: audio -> complex baseband IQ for TX.

Inverse of dsp/demod.py. Takes real audio at audio_rate and produces complex64
IQ at the radio wire_rate (39062.5 Hz for the PRO), ready to be packetized and
paced to the radio.

SSB is generated with a Hilbert transform (analytic signal):
    USB:  iq = analytic(audio)              (positive-frequency only)
    LSB:  iq = conj(analytic(audio))        (negative-frequency only)
This is the standard phasing/Weaver-equivalent method and is the exact inverse
of the demod's "USB = real part / LSB = imag part" convention, so a
modulate -> demodulate round-trip returns the original audio.

Runs streaming (block by block) with correct filter/resampler state carried
across blocks, so a live audio source can be modulated in real time.
"""

import numpy as np
from scipy import signal


class Modulator:
    def __init__(self, audio_rate=48000, wire_rate=39062.5, mode='USB'):
        self.audio_rate = int(audio_rate)
        self.wire_rate = float(wire_rate)
        self.mode = mode.upper()

        # Band-limit audio to the SSB voice passband before modulation.
        self._bpf = signal.butter(6, [300, 2700], btype='band',
                                  fs=self.audio_rate, output='sos')
        self._zi = signal.sosfilt_zi(self._bpf)

        # Polyphase resampler ratio audio_rate -> wire_rate.
        # We resample the analytic (complex) signal so the Hilbert phase is
        # preserved. up/down are the reduced integer ratio.
        from math import gcd
        g = gcd(int(round(self.wire_rate)), self.audio_rate)
        # wire_rate is fractional (39062.5); scale by 2 to make it integer.
        wr2 = int(round(self.wire_rate * 2))
        ar2 = self.audio_rate * 2
        g2 = gcd(wr2, ar2)
        self._up = wr2 // g2
        self._down = ar2 // g2

        # FM state
        self._fm_phase = 0.0
        self.fm_deviation = 2500.0  # Hz

        # TX gain / peak (keep within [-1, 1] for 24-bit packing headroom)
        self.gain = 0.9
        # Input audio may exceed [-1,1] (loud masters). A slow peak-tracking
        # normalizer keeps drive constant without the per-block level jumps a
        # hard clip would cause (clipping generates out-of-band harmonics).
        # Start at 0 so the fast attack locks onto the true signal peak on the
        # very first block (a 1.0 start would suppress a quiet source for many
        # release-time-constants before it levelled up).
        self._in_peak = 0.0

    def set_mode(self, mode):
        self.mode = mode.upper()

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Modulate one block of real audio -> complex64 IQ at wire_rate."""
        audio = np.asarray(audio, dtype=np.float64)
        if len(audio) == 0:
            return np.zeros(0, dtype=np.complex64)

        # Input leveling: track the running peak and scale toward full scale so
        # BOTH loud masters (peak > 1) and quiet sources (e.g. JS8Call/WSJT-X
        # with the audio slider well down, ~-25 dBFS) drive the modulator near
        # unity. The actual TX power is set by the drive byte downstream, so we
        # want a consistent, strong modulator input regardless of the app's
        # output volume. Fast attack (instant up to a louder peak) and a
        # moderately fast release (~0.3 s) so a quiet source levels UP within a
        # fraction of a second rather than staying suppressed for many seconds.
        # A noise gate avoids blowing up pure silence into full-scale hiss.
        blk_peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        if blk_peak > self._in_peak:
            self._in_peak = blk_peak           # fast attack
        else:
            # release: ~0.3 s time constant at 20 ms blocks -> 0.94/block
            self._in_peak = 0.94 * self._in_peak + 0.06 * blk_peak
        GATE = 1e-3   # below this, treat as silence — don't amplify noise
        TARGET = 0.9  # normalize peak up/down to ~0.9 full scale
        if self._in_peak > GATE:
            audio = audio * (TARGET / max(self._in_peak, GATE))

        m = self.mode
        if m in ('USB', 'LSB'):
            # Band-limit, form analytic signal, pick sideband.
            a, self._zi = signal.sosfilt(self._bpf, audio, zi=self._zi)
            analytic = signal.hilbert(a)  # complex, positive-freq content
            iq = analytic if m == 'USB' else np.conj(analytic)
        elif m == 'AM':
            # Double-sideband + carrier: (1 + m*audio) as a real envelope.
            a, self._zi = signal.sosfilt(self._bpf, audio, zi=self._zi)
            norm = a / (np.max(np.abs(a)) + 1e-9)
            iq = (0.5 + 0.5 * norm).astype(np.complex128)
        elif m == 'FM':
            # Integrate audio to phase.
            k = 2 * np.pi * self.fm_deviation / self.audio_rate
            phase = self._fm_phase + np.cumsum(audio * k)
            self._fm_phase = float(phase[-1] % (2 * np.pi))
            iq = np.exp(1j * phase)
        else:
            raise ValueError(f'unknown TX mode {m}')

        # Resample complex IQ from audio_rate to wire_rate (fractional-safe).
        n_out = int(round(len(iq) * self.wire_rate / self.audio_rate))
        if n_out > 0:
            iq = signal.resample(iq, n_out)

        iq = iq * self.gain
        # Clip to unit circle-ish range so 24-bit packing never wraps.
        peak = np.max(np.abs(iq)) if len(iq) else 0
        if peak > 0.98:
            iq = iq * (0.98 / peak)
        return iq.astype(np.complex64)
