"""
Tap/attenuator path-loss calibration: convert an SA peak reading to real watts.

Loads the loss table produced by tools/cal_tap.py (SSA tracking-generator
normalization) and provides:
    sa_dbm_to_watts(sa_peak_dbm, freq_hz) -> watts
    path_loss_db(freq_hz) -> interpolated loss in dB

  P_radio(dBm) = SA_peak(dBm) + path_loss(freq)
  watts        = 10 ** ((P_radio - 30) / 10)

Accuracy is bounded by the SSA's amplitude spec (~±1.5 dB -> ~±40% on watts) —
good for the amp-protection ceiling and relative work, not a lab wattmeter.
"""
import json
import os
from bisect import bisect_left

DEFAULT_CAL_PATH = os.path.expanduser('~/.config/solsdr/tap_cal.json')


class TapCal:
    def __init__(self, path: str = DEFAULT_CAL_PATH):
        self.path = path
        self.loss_db = {}   # freq_hz (int) -> loss dB
        self.load()

    def load(self):
        self.offset_db = 0.0  # wattmeter-anchor correction, added to every loss
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    raw = json.load(f)
                self.loss_db = {int(k): float(v)
                                for k, v in raw.get('loss_db', {}).items()}
                self.offset_db = float(raw.get('offset_db', 0.0))
            except (OSError, ValueError):
                self.loss_db = {}
                self.offset_db = 0.0

    @property
    def calibrated(self) -> bool:
        return len(self.loss_db) >= 1

    def path_loss_db(self, freq_hz: float) -> float:
        """Interpolate the path loss at freq_hz from the calibration points.

        Interpolates linearly in log10(freq) vs dB — the tap is a capacitive
        pickoff whose loss is ~linear in log-frequency (~-20 dB/decade), so
        log-f interpolation is physically correct and far better than
        linear-in-Hz between widely spaced HF anchors. Clamps to the nearest
        endpoint outside the calibrated range. Raises if uncalibrated.
        """
        import math
        if not self.loss_db:
            raise RuntimeError('tap not calibrated — run tools/tx_anchor.py')
        freqs = sorted(self.loss_db)
        if freq_hz <= freqs[0]:
            return self.loss_db[freqs[0]] + self.offset_db
        if freq_hz >= freqs[-1]:
            return self.loss_db[freqs[-1]] + self.offset_db
        i = bisect_left(freqs, freq_hz)
        f0, f1 = freqs[i - 1], freqs[i]
        l0, l1 = self.loss_db[f0], self.loss_db[f1]
        frac = (math.log10(freq_hz) - math.log10(f0)) / \
               (math.log10(f1) - math.log10(f0))
        return l0 + frac * (l1 - l0) + self.offset_db

    def sa_dbm_to_watts(self, sa_peak_dbm: float, freq_hz: float) -> float:
        """Convert an SA peak reading (dBm) at freq_hz to radio output watts."""
        p_radio_dbm = sa_peak_dbm + self.path_loss_db(freq_hz)
        return 10 ** ((p_radio_dbm - 30) / 10)

    def sa_dbm_to_dbm(self, sa_peak_dbm: float, freq_hz: float) -> float:
        """Radio output power in dBm (SA reading + path loss)."""
        return sa_peak_dbm + self.path_loss_db(freq_hz)
