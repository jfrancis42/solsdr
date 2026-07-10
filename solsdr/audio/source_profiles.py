"""Per-TX-source audio profiles: input gain + voice shaping, calibrated and
persisted per source (mic1 / mic2 / pc).

Each source the operator can transmit from has different characteristics — a
Yaesu hand mic on mic2 is already voiced for comms and runs near full level; a
studio/USB mic on mic1 or the PC sink is high-fidelity (picks up rumble,
proximity bass, sibilance) and often at a very different level. Rather than make
the operator guess a mic-gain number, we CALIBRATE each source (talk normally,
measure the peak, compute the gain that hits a headroom target) and SHAPE it for
intelligibility (a voice band-pass + speech compression preset). The result is
saved per source and re-applied automatically whenever the operator switches
sources, so mic1/mic2/pc each "just work" at their own settings.

A profile:
    gain        fixed linear input gain (used when calibrated -> leveling off)
    shape       voice-shaping preset name (see modulator.SHAPE_PRESETS)
    calibrated  True once `calibrate` has measured this source. Calibrated ->
                the modulator uses the fixed gain with auto-leveling OFF (output
                tracks the voice, like a real rig). Uncalibrated -> auto-leveling
                stays ON as a safety net and gain is advisory only.
"""
from ..dsp.modulator import SHAPE_PRESETS, DEFAULT_SHAPE

SOURCES = ('mic1', 'mic2', 'pc')

# Calibration aims the measured speech PEAK at this fraction of full scale,
# leaving headroom for louder syllables before the modulator's hard clip.
CAL_TARGET_PEAK = 0.7
# Below this measured peak, refuse to calibrate — the source is essentially
# silent (mic muted / wrong device / not talking), and a huge computed gain
# would just amplify noise.
CAL_MIN_PEAK = 0.005
# Cap the computed gain so a near-silent source can't produce an absurd value.
CAL_MAX_GAIN = 200.0

# Sensible per-source defaults BEFORE calibration. mic2 is assumed to be the
# Yaesu-style hand mic (already comms-voiced, near full level -> 'flat', gain 1);
# mic1 and pc are assumed studio/USB mics that benefit from 'comms' shaping.
_DEFAULTS = {
    'mic1': {'gain': 1.0, 'shape': 'comms', 'calibrated': False},
    'mic2': {'gain': 1.0, 'shape': 'flat',  'calibrated': False},
    'pc':   {'gain': 1.0, 'shape': 'comms', 'calibrated': False},
}


class SourceProfile:
    __slots__ = ('gain', 'shape', 'calibrated')

    def __init__(self, gain=1.0, shape=DEFAULT_SHAPE, calibrated=False):
        self.gain = float(gain)
        self.shape = shape if shape in SHAPE_PRESETS else DEFAULT_SHAPE
        self.calibrated = bool(calibrated)

    def to_dict(self):
        return {'gain': round(self.gain, 4), 'shape': self.shape,
                'calibrated': self.calibrated}

    def __repr__(self):
        cal = 'calibrated' if self.calibrated else 'uncal'
        return (f'gain={self.gain:.3g} shape={self.shape} {cal}')


class SourceProfiles:
    """Holds one SourceProfile per source, with config (de)serialization and
    the calibration math."""

    def __init__(self, profiles=None):
        self._p = {}
        for s in SOURCES:
            d = dict(_DEFAULTS[s])
            if profiles and s in profiles:
                d.update(profiles[s])
            self._p[s] = SourceProfile(**{k: d[k] for k in
                                          ('gain', 'shape', 'calibrated')})

    def get(self, source):
        return self._p[source] if source in self._p else self._p['pc']

    def set_gain(self, source, gain):
        p = self.get(source)
        p.gain = max(0.0, float(gain))
        return p

    def set_shape(self, source, shape):
        if shape not in SHAPE_PRESETS:
            return False
        self.get(source).shape = shape
        return True

    def calibrate_from_peak(self, source, measured_peak):
        """Compute + store the fixed gain that puts `measured_peak` at the
        headroom target. Returns (ok, gain_or_reason). Marks the source
        calibrated on success (so the modulator switches to fixed-gain / no
        auto-level for it)."""
        if measured_peak < CAL_MIN_PEAK:
            return (False, f'input too quiet (peak {measured_peak:.4f} < '
                    f'{CAL_MIN_PEAK}) — is the mic live and are you speaking?')
        gain = min(CAL_TARGET_PEAK / measured_peak, CAL_MAX_GAIN)
        p = self.get(source)
        p.gain = gain
        p.calibrated = True
        return True, gain

    # -- config (de)serialization ----------------------------------------
    def to_config(self):
        """Flat config keys: tx_src_<source>_{gain,shape,cal}."""
        out = {}
        for s in SOURCES:
            p = self._p[s]
            out[f'tx_src_{s}_gain'] = round(p.gain, 4)
            out[f'tx_src_{s}_shape'] = p.shape
            out[f'tx_src_{s}_cal'] = p.calibrated
        return out

    @classmethod
    def from_config(cls, cfg):
        """Build from a flat config dict (tx_src_<source>_{gain,shape,cal})."""
        profiles = {}
        for s in SOURCES:
            d = {}
            if f'tx_src_{s}_gain' in cfg:
                try:
                    d['gain'] = float(cfg[f'tx_src_{s}_gain'])
                except (ValueError, TypeError):
                    pass
            if f'tx_src_{s}_shape' in cfg:
                d['shape'] = str(cfg[f'tx_src_{s}_shape'])
            if f'tx_src_{s}_cal' in cfg:
                v = cfg[f'tx_src_{s}_cal']
                d['calibrated'] = v in (True, 'true', 'True', '1', 1, 'yes')
            if d:
                profiles[s] = d
        return cls(profiles)
