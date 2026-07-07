"""RX DSP Chain - Signal processing for received IQ"""

import numpy as np
from scipy import signal
from typing import Optional


class RXDSPChain:
    """Complete RX DSP processing chain"""

    def __init__(self, radio_rate=312500, audio_rate=48000):
        """
        Initialize DSP chain

        Args:
            radio_rate: Input sample rate from radio (Hz)
            audio_rate: Output audio sample rate (Hz)
        """
        self.radio_rate = radio_rate
        self.audio_rate = audio_rate

        # Calculate decimation
        self.decim = int(radio_rate / audio_rate)
        self.intermediate_rate = radio_rate / self.decim

        # Design decimation filter
        # Cutoff at audio_rate/2 to prevent aliasing
        self.decim_filter = signal.butter(
            8,  # Order
            audio_rate / 2,  # Cutoff frequency
            fs=radio_rate,
            output='sos'
        )

        # IQ buffer for processing
        self.iq_buffer = np.array([], dtype=np.complex64)

        # AGC state
        self.agc_gain = 1.0
        self.agc_target = 0.3  # Target RMS level
        self.agc_attack = 0.001
        self.agc_decay = 0.1

    def process_iq(self, iq_samples: np.ndarray, mode: str = 'USB') -> Optional[np.ndarray]:
        """
        Process IQ samples to audio

        Args:
            iq_samples: Input IQ samples (complex64)
            mode: Demodulation mode ('USB', 'LSB', 'AM', 'FM', 'CW')

        Returns:
            Audio samples at audio_rate, or None if not enough data yet
        """
        # Add to buffer
        self.iq_buffer = np.concatenate([self.iq_buffer, iq_samples])

        audio_chunks = []

        # Process in chunks to avoid memory issues
        chunk_size = 6000
        while len(self.iq_buffer) >= chunk_size:
            chunk = self.iq_buffer[:chunk_size]
            self.iq_buffer = self.iq_buffer[chunk_size:]

            # 1. Decimate
            decimated = self._decimate(chunk)

            # 2. Demodulate
            audio = self._demodulate(decimated, mode)

            # 3. Resample to exact audio rate
            audio = self._resample(audio)

            # 4. AGC
            audio = self._agc(audio)

            # 5. Clip
            audio = np.clip(audio, -0.95, 0.95)

            audio_chunks.append(audio.astype(np.float32))

        if audio_chunks:
            return np.concatenate(audio_chunks)
        return None

    def _decimate(self, iq: np.ndarray) -> np.ndarray:
        """Decimate IQ samples with anti-aliasing filter"""
        # Apply anti-aliasing filter
        filtered = signal.sosfilt(self.decim_filter, iq)

        # Decimate (keep every Nth sample)
        decimated = filtered[::self.decim]

        return decimated

    def _demodulate(self, iq: np.ndarray, mode: str) -> np.ndarray:
        """
        Demodulate IQ to audio

        Args:
            iq: Input IQ samples
            mode: Demodulation mode

        Returns:
            Audio samples (real)
        """
        if mode == 'USB':
            # USB: signal in positive frequencies
            # Simple approach: take real part (assumes centered)
            audio = iq.real

        elif mode == 'LSB':
            # LSB: signal in negative frequencies
            # Conjugate then take real, or take imag
            audio = iq.imag

        elif mode == 'AM':
            # AM: envelope detection
            audio = np.abs(iq)
            # Remove DC
            audio = audio - np.mean(audio)

        elif mode == 'FM':
            # FM: phase demodulation
            # diff(angle(iq))
            phase = np.angle(iq)
            audio = np.diff(phase)
            # Unwrap phase jumps
            audio = np.concatenate([[0], audio])

        elif mode == 'CW':
            # CW: similar to SSB but with tighter filter
            # For now, same as USB
            audio = iq.real

        else:
            raise ValueError(f"Unknown mode: {mode}")

        return audio

    def _resample(self, audio: np.ndarray) -> np.ndarray:
        """Resample to exact audio rate"""
        # Calculate output length
        ratio = self.audio_rate / self.intermediate_rate
        num_out = int(len(audio) * ratio)

        # Resample using scipy
        if num_out > 0:
            audio = signal.resample(audio, num_out)

        return audio

    def _agc(self, audio: np.ndarray) -> np.ndarray:
        """
        Apply AGC (Automatic Gain Control)

        Simple RMS-based AGC with attack/decay
        """
        # Calculate RMS
        rms = np.sqrt(np.mean(audio**2))

        if rms < 1e-6:
            # Silence - don't change gain
            return audio

        # Target gain
        target_gain = self.agc_target / rms

        # Attack/decay
        if target_gain > self.agc_gain:
            # Increase gain slowly (decay)
            alpha = self.agc_decay
        else:
            # Decrease gain quickly (attack)
            alpha = self.agc_attack

        # Smooth gain changes
        self.agc_gain = alpha * target_gain + (1 - alpha) * self.agc_gain

        # Limit gain range
        self.agc_gain = np.clip(self.agc_gain, 0.1, 100.0)

        # Apply gain
        return audio * self.agc_gain

    def set_agc_params(self, target: float = 0.3, attack: float = 0.001, decay: float = 0.1):
        """
        Set AGC parameters

        Args:
            target: Target RMS level (0.0 to 1.0)
            attack: Attack time constant (0.0 to 1.0, lower = faster)
            decay: Decay time constant (0.0 to 1.0, lower = faster)
        """
        self.agc_target = target
        self.agc_attack = attack
        self.agc_decay = decay

    def reset(self):
        """Reset DSP state"""
        self.iq_buffer = np.array([], dtype=np.complex64)
        self.agc_gain = 1.0
