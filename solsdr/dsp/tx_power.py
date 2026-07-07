"""
Per-band TX power calibration: watts <-> drive byte (0x17).

The radio's drive byte (0-255) maps to actual RF output through a per-band,
nonlinear PA curve that ExpertSDR3 calibrates against a wattmeter. There is no
universal formula (the ArtemisSDR source is explicit that any fixed LUT
double-compensates). So this table is EMPIRICAL: you add measured
(drive_byte, watts) points per band during a wattmeter calibration session, and
watts_to_drive() interpolates within them.

Until a band has measured points, it falls back to a voltage-domain sqrt model
against an assumed full-scale power. That model is a STARTING ESTIMATE ONLY —
it can be off by ~2x and must be verified with a wattmeter before trusting it.

Bands are keyed by name (e.g. '20m'); band_for_freq() maps a frequency to its
ham band. The table persists to JSON so calibration accumulates across sessions.
"""

import json
import os
from bisect import bisect_left
from typing import Dict, List, Optional, Tuple

# Assumed full-scale power for the sqrt fallback model (PRO ~100 W). Only used
# for UNCALIBRATED bands; measured points override it entirely.
DEFAULT_FULL_SCALE_W = 100.0

# HF/6m ham band edges (Hz) -> band name. Used to pick the calibration bucket.
_BANDS = [
    ('160m', 1_800_000, 2_000_000),
    ('80m', 3_500_000, 4_000_000),
    ('60m', 5_330_000, 5_410_000),
    ('40m', 7_000_000, 7_300_000),
    ('30m', 10_100_000, 10_150_000),
    ('20m', 14_000_000, 14_350_000),
    ('17m', 18_068_000, 18_168_000),
    ('15m', 21_000_000, 21_450_000),
    ('12m', 24_890_000, 24_990_000),
    ('10m', 28_000_000, 29_700_000),
    ('6m', 50_000_000, 54_000_000),
]

DEFAULT_TABLE_PATH = os.path.expanduser('~/.config/solsdr/tx_power_cal.json')


def band_for_freq(freq_hz: int) -> str:
    """Return the ham-band name for a frequency, or 'unknown' if out of band.

    'unknown' still calibrates as its own bucket so out-of-band test points
    aren't lost, but prefer a named band.
    """
    for name, lo, hi in _BANDS:
        if lo <= freq_hz <= hi:
            return name
    return 'unknown'


def sqrt_model_drive(watts: float, full_scale_w: float = DEFAULT_FULL_SCALE_W) -> int:
    """Voltage-domain sqrt estimate: byte = 255 * sqrt(watts/full_scale).

    STARTING ESTIMATE ONLY. Example anchors at 100 W full scale:
      2.5 W -> 40, 3.5 W -> 48, 5 W -> 57, 100 W -> 255.
    """
    if watts <= 0:
        return 0
    frac = (watts / full_scale_w) ** 0.5
    return max(0, min(255, round(frac * 255)))


class TXPowerCal:
    """Per-band calibration table with linear interpolation + sqrt fallback."""

    def __init__(self, path: str = DEFAULT_TABLE_PATH,
                 full_scale_w: float = DEFAULT_FULL_SCALE_W):
        self.path = path
        self.full_scale_w = full_scale_w
        # band -> list of (drive_byte, watts), kept sorted by drive_byte
        self.points: Dict[str, List[Tuple[int, float]]] = {}
        self.load()

    # -- persistence -------------------------------------------------------
    def load(self):
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    raw = json.load(f)
                self.points = {b: sorted((int(d), float(w)) for d, w in pts)
                               for b, pts in raw.get('points', {}).items()}
                self.full_scale_w = raw.get('full_scale_w', self.full_scale_w)
            except (OSError, ValueError, KeyError):
                self.points = {}

    def save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'w') as f:
            json.dump({'full_scale_w': self.full_scale_w,
                       'points': {b: [[d, w] for d, w in pts]
                                  for b, pts in self.points.items()}}, f, indent=2)

    # -- calibration data --------------------------------------------------
    def add_measurement(self, band: str, drive_byte: int, watts: float,
                        save: bool = True):
        """Record a measured (drive_byte -> watts) point for a band."""
        pts = self.points.setdefault(band, [])
        # replace any existing point at the same drive byte
        pts = [(d, w) for d, w in pts if d != drive_byte]
        pts.append((int(drive_byte), float(watts)))
        self.points[band] = sorted(pts)
        if save:
            self.save()

    def is_calibrated(self, band: str) -> bool:
        return len(self.points.get(band, [])) >= 2

    # -- lookups -----------------------------------------------------------
    def watts_to_drive(self, watts: float, freq_hz: int) -> Tuple[int, bool]:
        """Return (drive_byte, calibrated?) for a target watts on freq's band.

        Uses linear interpolation across measured points when the band has
        >=2 of them; otherwise the sqrt model estimate (calibrated=False).
        """
        band = band_for_freq(freq_hz)
        pts = self.points.get(band, [])
        if len(pts) >= 2:
            # interpolate watts (y) vs drive (x); invert to get x for target y.
            # points sorted by drive; watts should be monotonic in drive.
            drives = [d for d, _ in pts]
            watts_pts = [w for _, w in pts]
            if watts <= watts_pts[0]:
                return pts[0][0], True
            if watts >= watts_pts[-1]:
                return pts[-1][0], True
            i = bisect_left(watts_pts, watts)
            w0, w1 = watts_pts[i - 1], watts_pts[i]
            d0, d1 = drives[i - 1], drives[i]
            frac = (watts - w0) / (w1 - w0) if w1 != w0 else 0
            return round(d0 + frac * (d1 - d0)), True
        return sqrt_model_drive(watts, self.full_scale_w), False

    def drive_to_watts(self, drive_byte: int, freq_hz: int) -> Tuple[float, bool]:
        """Estimate watts for a drive byte on freq's band (inverse lookup)."""
        band = band_for_freq(freq_hz)
        pts = self.points.get(band, [])
        if len(pts) >= 2:
            drives = [d for d, _ in pts]
            watts_pts = [w for _, w in pts]
            if drive_byte <= drives[0]:
                return watts_pts[0], True
            if drive_byte >= drives[-1]:
                return watts_pts[-1], True
            i = bisect_left(drives, drive_byte)
            d0, d1 = drives[i - 1], drives[i]
            w0, w1 = watts_pts[i - 1], watts_pts[i]
            frac = (drive_byte - d0) / (d1 - d0) if d1 != d0 else 0
            return w0 + frac * (w1 - w0), True
        # invert sqrt model
        frac = drive_byte / 255.0
        return frac * frac * self.full_scale_w, False
