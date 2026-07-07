"""
Multi-mode demodulator for SunSDR2 baseband IQ.

Input: complex64 IQ centered on the tuned frequency, at the radio wire rate
(39062.5 Hz for the PRO). Output: real float32 audio at audio_rate (48 kHz).

Modes: USB, LSB, AM, FM, CW (CW = USB with a narrow filter).

Also provides an S-meter (dBFS of the IQ power) so callers can display signal
strength without a second pass over the data.
"""

from math import gcd

import numpy as np
from scipy import signal


class _StreamResampler:
    """Rational resampler that decimates a high wire rate down to audio rate
    with a proper anti-alias FIR, avoiding the per-block FFT circular-
    convolution artifact of signal.resample() that smeared tones at high
    decimation ratios (silently broke FT8 decode at the 312.5 kHz / ~26:1 ratio).

    Strategy: first INTEGER-decimate the wire signal toward the audio rate with
    a stateful FIR (signal.decimate-style, but state carried across blocks),
    then do the small fractional resample_poly to hit the exact audio rate.
    Integer decimation is where the aliasing risk is, and doing it with a real
    low-pass FIR (not FFT) is what fixes the high-rate case. The residual
    fractional step is a gentle ratio (< 4:1) where resample_poly is clean.
    """

    def __init__(self, in_rate: float, out_rate: float):
        self.in_rate = float(in_rate)
        self.out_rate = float(out_rate)
        # Single-stage polyphase resample on the reduced full ratio. For the
        # PRO rates -> 12 kHz/48 kHz these reduce to a small up and down=625,
        # which resample_poly handles efficiently (measured >3x realtime even
        # at 312.5 kHz with small blocks, ~20x with larger blocks) and cleanly
        # (proper anti-alias FIR, no FFT circular-convolution smearing). The
        # caller should feed reasonably large blocks (see Demodulator batching)
        # so the per-call filter setup amortizes.
        num = int(round(self.out_rate * 2))
        den = int(round(self.in_rate * 2))
        g = gcd(num, den)
        self._up = num // g
        self._down = den // g

    def process(self, x: np.ndarray) -> np.ndarray:
        if len(x) == 0:
            return x
        if self._up == self._down:
            return x.astype(np.float64)
        return signal.resample_poly(x.astype(np.float64), self._up, self._down)


class Demodulator:
    def __init__(self, wire_rate=39062.5, audio_rate=48000, mode='USB',
                 agc='auto', cw_pitch=600.0, cw_bandwidth=250.0):
        """
        agc: 'auto'  -> AGC on for voice modes (USB/LSB/AM/FM), off for CW/data
             'on'    -> always AGC
             'off'   -> linear (best for data modes fed to FT8/FT4 decoders;
                        AGC distorts the relative amplitudes of the ~50
                        simultaneous FT8 tones and measurably lowers decode
                        count — verified 57->63 decodes with AGC off)
             'fixed:<gain>' -> constant linear gain

        cw_pitch: CW beat-note pitch in Hz. In CW mode a digital BFO shifts the
            on-frequency signal to this audible tone, so the operator tunes the
            radio directly ON the signal (frequency readout stays honest) and
            hears it at cw_pitch — unlike a plain-USB CW where you must tune
            cw_pitch away. CWU = USB side, CWL = LSB side (reversed BFO).
        cw_bandwidth: CW filter bandwidth in Hz (e.g. 250 for a tight CW filter,
            50-100 for very weak/crowded, 500 for wide/fast). Centered on pitch.
        """
        self.wire_rate = float(wire_rate)
        self.audio_rate = int(audio_rate)
        self.mode = mode.upper()
        self.agc_mode = agc
        self.cw_pitch = float(cw_pitch)
        self.cw_bandwidth = float(cw_bandwidth)

        # SSB voice passband; CW uses a narrow filter around the CW pitch.
        self._design_filters()

        # Stateful polyphase resampler wire_rate -> audio_rate (replaces the
        # per-block FFT resample that smeared tones at high decimation ratios).
        self._resampler = _StreamResampler(self.wire_rate, self.audio_rate)

        # AGC state
        self.agc_gain = 1.0
        self.agc_target = 0.15
        self.agc_max = 5e5
        self.fixed_gain = 3000.0  # linear-mode default; scaled 24-bit -> ~unity

        # FM state
        self._fm_prev = 0.0 + 0.0j
        # CW BFO phase (radians), carried across blocks for continuity
        self._cw_phase = 0.0

        # S-meter state (smoothed)
        self.smeter_dbfs = -120.0

    def _design_filters(self):
        nyq = self.wire_rate / 2
        # SSB / CW audio-band filters expressed at the wire rate.
        def bp(lo, hi):
            lo = max(lo, 1.0)
            hi = min(hi, nyq * 0.98)
            return signal.butter(6, [lo, hi], btype='band',
                                 fs=self.wire_rate, output='sos')
        self._ssb = bp(300, 2700)        # standard SSB voice passband
        # CW: narrow bandpass centered on the beat-note pitch, width = cw_bandwidth
        half = max(25.0, self.cw_bandwidth / 2)
        self._cw = bp(self.cw_pitch - half, self.cw_pitch + half)
        self._am = signal.butter(6, 5000, btype='low',
                                 fs=self.wire_rate, output='sos')
        self._zi_ssb = signal.sosfilt_zi(self._ssb)
        self._zi_cw = signal.sosfilt_zi(self._cw)
        self._zi_am = signal.sosfilt_zi(self._am)

    def set_cw(self, pitch=None, bandwidth=None):
        """Adjust CW pitch and/or filter bandwidth (Hz) and rebuild the filter."""
        if pitch is not None:
            self.cw_pitch = float(pitch)
        if bandwidth is not None:
            self.cw_bandwidth = float(bandwidth)
        self._design_filters()

    def set_mode(self, mode: str):
        self.mode = mode.upper()

    def _update_smeter(self, iq):
        p = np.mean(np.abs(iq) ** 2)
        if p > 0:
            db = 10 * np.log10(p)
            # smooth
            self.smeter_dbfs = 0.8 * self.smeter_dbfs + 0.2 * db
        return self.smeter_dbfs

    def _agc_active(self):
        """Resolve whether AGC should run for the current mode."""
        m = self.agc_mode
        if m == 'on':
            return True
        if m == 'off' or m.startswith('fixed:'):
            return False
        # 'auto': AGC for listening modes (voice + CW), linear for wide data
        # modes fed to external decoders (FT8/FT4). CW needs AGC so the fixed
        # 24-bit-scale gain doesn't clip the beat note into a constant carrier
        # (which erases the keying the Morse decoder relies on).
        return self.mode in ('USB', 'LSB', 'AM', 'FM', 'CW', 'CWU', 'CWL')

    def _gain(self, audio):
        if self._agc_active():
            rms = np.sqrt(np.mean(audio ** 2)) if len(audio) else 0.0
            if rms > 1e-9:
                target = self.agc_target / rms
                self.agc_gain = 0.9 * self.agc_gain + 0.1 * target
                self.agc_gain = float(np.clip(self.agc_gain, 1.0, self.agc_max))
            return np.clip(audio * self.agc_gain, -0.98, 0.98)
        # linear
        if self.agc_mode.startswith('fixed:'):
            g = float(self.agc_mode.split(':', 1)[1])
        else:
            g = self.fixed_gain
        return np.clip(audio * g, -0.98, 0.98)

    def process(self, iq: np.ndarray) -> np.ndarray:
        """Demodulate one block of IQ to float32 audio at audio_rate."""
        self._update_smeter(iq)
        m = self.mode

        if m == 'USB':
            audio = iq.real.astype(np.float64)
            audio, self._zi_ssb = signal.sosfilt(self._ssb, audio, zi=self._zi_ssb)
        elif m == 'LSB':
            audio = iq.imag.astype(np.float64)
            audio, self._zi_ssb = signal.sosfilt(self._ssb, audio, zi=self._zi_ssb)
        elif m in ('CW', 'CWU', 'CWL'):
            # Digital BFO: the CW signal sits at the tuned frequency (near DC in
            # the baseband IQ). Mixing by a complex exponential at cw_pitch
            # shifts it up to an audible beat note, so the operator tunes ON the
            # signal (honest frequency readout) and hears it at cw_pitch. CWL
            # uses the opposite mixing sign (lower sideband). Phase is carried
            # across blocks so there's no click at block boundaries.
            n = len(iq)
            sign = -1.0 if m == 'CWL' else 1.0
            dphi = sign * 2 * np.pi * self.cw_pitch / self.wire_rate
            ph = self._cw_phase + dphi * np.arange(n)
            audio = (iq * np.exp(1j * ph)).real.astype(np.float64)
            self._cw_phase = float((self._cw_phase + dphi * n) % (2 * np.pi))
            # Narrow bandpass centered on the pitch pulls the note out of noise.
            audio, self._zi_cw = signal.sosfilt(self._cw, audio, zi=self._zi_cw)
        elif m == 'AM':
            env = np.abs(iq).astype(np.float64)
            env, self._zi_am = signal.sosfilt(self._am, env, zi=self._zi_am)
            audio = env - np.mean(env)          # remove DC/carrier
        elif m == 'FM':
            # Quadrature FM discriminator (phase difference)
            x = iq.astype(np.complex128)
            prev = np.empty_like(x)
            prev[0] = self._fm_prev
            prev[1:] = x[:-1]
            self._fm_prev = x[-1]
            audio = np.angle(x * np.conj(prev))
        else:
            raise ValueError(f'unknown mode {m}')

        # Resample wire_rate -> audio_rate with the stateful polyphase resampler
        # (glitch-free across blocks; correct at all supported wire rates).
        audio = self._resampler.process(audio)

        audio = self._gain(audio)
        return audio.astype(np.float32)

    @property
    def s_meter(self) -> float:
        """Current smoothed signal level in dBFS."""
        return self.smeter_dbfs
