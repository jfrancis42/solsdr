"""
SSB / AM / FM modulator: audio -> complex baseband IQ for TX.

Inverse of dsp/demod.py. Takes real audio at audio_rate and produces complex64
IQ at the radio wire_rate (39062.5 Hz for the PRO), ready to be packetized and
paced to the radio.

SSB is generated with a Hilbert transform (analytic signal). NOTE the sideband
selection is INVERTED from the textbook baseband convention because the SunSDR2's
TX upconverter mirrors the sideband (verified on-air 2026-07-09 against an SSA):
    USB:  iq = conj(analytic(audio))        (so a +tone lands ABOVE the carrier)
    LSB:  iq = analytic(audio)
The RX path is not inverted; this flip is specific to the radio's TX mixer.
This is the standard phasing/Weaver-equivalent method and is the exact inverse
of the demod's "USB = real part / LSB = imag part" convention, so a
modulate -> demodulate round-trip returns the original audio.

Runs streaming (block by block) with correct filter/resampler state carried
across blocks, so a live audio source can be modulated in real time.
"""

import numpy as np
from scipy import signal


# Per-source voice shaping presets. SSB is a COMMS mode, not hi-fi: shape the
# audio for intelligibility and talk power, not fidelity. Each preset is a
# voice band-pass (HPF removes rumble/proximity-bass/hum; LPF caps the ~3 kHz
# SSB channel) plus soft-clip speech COMPRESSION in dB (raises average power for
# a given peak = more punch; 0 = none). Applied HPF -> compress -> LPF so the
# clipper's harmonics are filtered off before transmit.
#   flat  — light touch, for a mic already voiced for comms (e.g. a Yaesu hand
#           mic on mic2). Just a gentle voice band-pass, no compression.
#   comms — default for a studio/PC mic: tighter band + moderate compression to
#           tame rumble/sibilance and add punch.
#   dx    — aggressive compression + tight band for maximum readability in a
#           pileup. Sounds processed, cuts through.
SHAPE_PRESETS = {
    'flat':  {'hpf': 300, 'lpf': 2700, 'comp_db': 0.0},
    'comms': {'hpf': 250, 'lpf': 2800, 'comp_db': 6.0},
    'dx':    {'hpf': 350, 'lpf': 2700, 'comp_db': 12.0},
}
DEFAULT_SHAPE = 'comms'


class Modulator:
    def __init__(self, audio_rate=48000, wire_rate=39062.5, mode='USB',
                 leveling=True, input_gain=1.0, shape=DEFAULT_SHAPE):
        self.audio_rate = int(audio_rate)
        self.wire_rate = float(wire_rate)
        self.mode = mode.upper()
        # Input-conditioning policy (voice/data modes; CW is always pass-through):
        #   leveling=True  -> auto-level the input peak toward ~0.9 (fast attack,
        #                     slow release). Good for an UNCALIBRATED source whose
        #                     level is unknown (e.g. a JS8Call slider). Ignores
        #                     input_gain.
        #   leveling=False -> apply a FIXED linear input_gain and do NOT auto-
        #                     level, so output tracks the operator's voice like a
        #                     normal rig's mic gain. Set by per-source calibration.
        # Both live-settable (a keyed session can flip policy mid-over).
        self.leveling = bool(leveling)
        self.input_gain = float(input_gain)

        # Voice shaping (see SHAPE_PRESETS). Band-limit + speech compression,
        # applied before modulation. _build_shaper() sets self._bpf/_zi/_comp_*.
        self.shape = shape if shape in SHAPE_PRESETS else DEFAULT_SHAPE
        self._build_shaper()

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

    def _build_shaper(self):
        """(Re)build the voice band-pass + compression from the current shape
        preset. Preserves nothing across a rebuild except the preset itself, so
        it's safe to call live between overs (not mid-block)."""
        p = SHAPE_PRESETS[self.shape]
        # Clamp corners to a valid band inside Nyquist.
        nyq = self.audio_rate / 2.0
        hpf = max(50.0, min(float(p['hpf']), nyq * 0.9))
        lpf = max(hpf + 100.0, min(float(p['lpf']), nyq * 0.95))
        self._bpf = signal.butter(6, [hpf, lpf], btype='band',
                                  fs=self.audio_rate, output='sos')
        self._zi = signal.sosfilt_zi(self._bpf)
        # Compression as a make-up gain into a soft clipper: comp_db of extra
        # gain before tanh soft-clip raises average power; the clipper's
        # products are then removed by the LPF stage (we band-pass AGAIN after
        # clipping). 0 dB => unity, no clip.
        self._comp_db = float(p['comp_db'])
        self._comp_gain = 10.0 ** (self._comp_db / 20.0)

    def set_shape(self, shape):
        """Select a voice-shaping preset ('flat'/'comms'/'dx'). Returns True if
        applied, False if the name is unknown."""
        if shape not in SHAPE_PRESETS:
            return False
        self.shape = shape
        self._build_shaper()
        return True

    def set_input(self, gain=None, leveling=None):
        """Set the input-conditioning policy live. gain: fixed linear input
        gain (used when leveling is off). leveling: True=auto-level (ignore
        gain), False=fixed gain. Either may be None to leave unchanged."""
        if gain is not None:
            self.input_gain = float(gain)
        if leveling is not None:
            self.leveling = bool(leveling)

    def _compress(self, audio):
        """Soft-clip speech compression: apply make-up gain then tanh, which
        gently limits peaks while lifting the average (more talk power). The
        post-clip band-pass (applied by the caller) removes the harmonics."""
        if self._comp_gain <= 1.0:
            return audio
        return np.tanh(audio * self._comp_gain)

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Modulate one block of real audio -> complex64 IQ at wire_rate."""
        audio = np.asarray(audio, dtype=np.float64)
        if len(audio) == 0:
            return np.zeros(0, dtype=np.complex64)

        # Input conditioning (voice/data modes only). NOT for CW: there the input
        # is an exact 0..1 keying envelope — gain/level/compression would distort
        # the keying and a gate would erase the raised-cosine ramps. CW passes
        # through unscaled.
        if self.mode not in ('CW', 'CWU', 'CWL'):
            if self.leveling:
                # UNCALIBRATED source: auto-level the peak toward full scale so
                # both loud masters (peak>1) and quiet sources (JS8Call slider
                # down) reach the modulator near unity. Fast attack, ~0.3 s
                # release; a gate avoids blowing silence up into full-scale hiss.
                blk_peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
                if blk_peak > self._in_peak:
                    self._in_peak = blk_peak           # fast attack
                else:
                    self._in_peak = 0.94 * self._in_peak + 0.06 * blk_peak
                GATE = 1e-3
                TARGET = 0.9
                if self._in_peak > GATE:
                    audio = audio * (TARGET / max(self._in_peak, GATE))
            else:
                # CALIBRATED source: fixed linear input gain, no auto-leveling —
                # output tracks the operator's voice like a normal rig mic gain.
                if self.input_gain != 1.0:
                    audio = audio * self.input_gain

        m = self.mode
        if m in ('USB', 'LSB'):
            # Voice shaping (comms, not hi-fi): HPF (rumble/proximity/hum) ->
            # speech compression (talk power) -> LPF, via the shaper band-pass
            # applied around the compressor. The band-pass is a single SOS run;
            # we compress between two half-runs would need split filters, so
            # instead: band-pass, compress, then band-pass again to clean the
            # clipper products. For 0 dB compression _compress is a no-op and the
            # second pass is skipped, preserving the original single-BP behavior.
            a, self._zi = signal.sosfilt(self._bpf, audio, zi=self._zi)
            if self._comp_gain > 1.0:
                a = self._compress(a)
                # Re-band-pass to remove soft-clip harmonics (fresh zi: this is a
                # cleanup pass, transient error is negligible on continuous voice).
                a = signal.sosfilt(self._bpf, a)
            analytic = signal.hilbert(a)  # complex, positive-freq content
            # NOTE: the SunSDR2's TX upconversion mirrors the sideband relative
            # to the modulator's baseband sense (verified on-air 2026-07-09 vs.
            # an SSA on the TX tap: a +1 kHz USB tone landed BELOW the carrier,
            # LSB above). The RX path is NOT inverted (FT8 decodes), so this is a
            # TX-mixer sign specific to the radio, not a math error here. So USB
            # takes the CONJUGATE (negative-freq analytic) and LSB the plain
            # analytic — the opposite of the textbook baseband convention — which
            # puts a USB tone correctly ABOVE the carrier out of the antenna.
            iq = np.conj(analytic) if m == 'USB' else analytic
        elif m in ('CW', 'CWU', 'CWL'):
            # CW: the input `audio` is a real 0..1 KEYING ENVELOPE (from
            # CWEncoder.envelope), not a tone. Emit an on/off carrier at
            # BASEBAND DC so the transmitted RF sits EXACTLY on the tuned
            # (dial) frequency — no sidetone offset. Bypasses the SSB voice
            # bandpass (a DC-keyed signal wouldn't survive 300-2700 Hz). The
            # envelope's raised-cosine edges keep it click-free.
            env = np.clip(audio.astype(np.float64), 0.0, 1.0)
            iq = env.astype(np.complex128)      # real carrier at 0 Hz, keyed
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
