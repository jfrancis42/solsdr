"""
Morse (CW) decoder: demodulated CW audio -> text.

Pipeline:
  1. Tone-power envelope: band-pass around the CW pitch, rectify, smooth.
  2. Adaptive key-state detection: a hysteresis threshold tracks the noise
     floor and signal peak, yielding key-down / key-up intervals.
  3. Timing -> elements: classify key-down as dot/dash and key-up as
     intra-char / inter-char / word gaps, with an adaptively estimated dot
     length (WPM), since senders vary and drift.
  4. Elements -> text via the Morse table.

Streaming: feed audio blocks to process(); completed characters/words are
emitted as they resolve. This is deliberately robust rather than fancy — it
decodes clean-to-moderate CW reliably, which is what we validate against by
generating known text at a known WPM and decoding it back.
"""

import numpy as np
from scipy import signal

MORSE = {
    '.-': 'A', '-...': 'B', '-.-.': 'C', '-..': 'D', '.': 'E', '..-.': 'F',
    '--.': 'G', '....': 'H', '..': 'I', '.---': 'J', '-.-': 'K', '.-..': 'L',
    '--': 'M', '-.': 'N', '---': 'O', '.--.': 'P', '--.-': 'Q', '.-.': 'R',
    '...': 'S', '-': 'T', '..-': 'U', '...-': 'V', '.--': 'W', '-..-': 'X',
    '-.--': 'Y', '--..': 'Z',
    '-----': '0', '.----': '1', '..---': '2', '...--': '3', '....-': '4',
    '.....': '5', '-....': '6', '--...': '7', '---..': '8', '----.': '9',
    '.-.-.-': '.', '--..--': ',', '..--..': '?', '-..-.': '/', '-.--.': '(',
    '-.--.-': ')', '---...': ':', '.----.': "'", '-....-': '-', '.-.-.': '+',
    '.-...': '&', '...-..-': '$', '.--.-.': '@', '-...-': '=',
}


def wpm_to_dot_seconds(wpm):
    """PARIS standard: dot = 1.2 / wpm seconds."""
    return 1.2 / wpm


# Reverse table for encoding: char -> morse code string.
TEXT_TO_MORSE = {v: k for k, v in MORSE.items()}


class CWEncoder:
    """Text -> CW audio (tone), with Farnsworth timing.

    Two speeds:
      * char_wpm: the speed of the dits/dahs WITHIN a character (element timing).
      * word_wpm: the overall speed that inter-character and inter-word spacing
        is stretched to. When word_wpm < char_wpm you get Farnsworth: characters
        are sent fast but with extra space between them, the standard method for
        learning/head-copy. word_wpm defaults to char_wpm (standard timing).

    Farnsworth spacing follows the ARRL method: the total extra delay for the
    3-unit (inter-char) + 7-unit (inter-word) gaps of the word PARIS is computed
    at word_wpm and distributed 3:4 between character and word gaps.
    """
    def __init__(self, sample_rate=48000, pitch=600.0, char_wpm=20.0,
                 word_wpm=None, amplitude=0.7, rise_ms=5.0):
        self.sample_rate = float(sample_rate)
        self.pitch = float(pitch)
        self.char_wpm = float(char_wpm)
        self.word_wpm = float(word_wpm) if word_wpm else float(char_wpm)
        self.amplitude = float(amplitude)
        self.rise_ms = float(rise_ms)

    def _timing(self):
        """Return (dot, dash, intra_gap, char_gap, word_gap) in seconds.

        Element timing uses char_wpm. Inter-char/word gaps use Farnsworth: the
        total space time for one PARIS word at word_wpm minus what the fast
        characters already consume, distributed 3:4 (ARRL) across the 4
        inter-char gaps and 1 inter-word gap.
        """
        u = wpm_to_dot_seconds(self.char_wpm)     # element unit (fast)
        dot, dash, intra = u, 3 * u, u
        if self.word_wpm >= self.char_wpm:
            return dot, dash, intra, 3 * u, 7 * u   # standard timing
        # Farnsworth (ARRL): total time for "PARIS " at word_wpm
        t_word = 60.0 / self.word_wpm               # seconds per word
        # PARIS = 50 units total; 31 units are elements+intra (sent at char_wpm),
        # 19 units are the inter-char (3*3) + inter-word (7) spacing we stretch.
        t_chars = 31 * u                            # element time at char speed
        t_space = t_word - t_chars                  # remaining time for spacing
        if t_space < 0:
            return dot, dash, intra, 3 * u, 7 * u
        # distribute: ARRL uses 3 units per char gap, 7 per word gap; there are
        # (per PARIS) the equivalent of 3 char-gaps *3u + 1 word-gap*7u = 16u...
        # simpler & standard: char_gap = 3 * ta, word_gap = 7 * ta, where ta is
        # the Farnsworth unit = t_space / 19.
        ta = t_space / 19.0
        return dot, dash, intra, 3 * ta, 7 * ta

    def _tone(self, seconds):
        n = int(round(seconds * self.sample_rate))
        if n <= 0:
            return np.zeros(0, dtype=np.float64)
        t = np.arange(n) / self.sample_rate
        sig = self.amplitude * np.sin(2 * np.pi * self.pitch * t)
        # raised-cosine keying envelope to avoid key clicks
        r = int(self.sample_rate * self.rise_ms / 1000.0)
        if r > 0 and 2 * r < n:
            ramp = 0.5 * (1 - np.cos(np.pi * np.arange(r) / r))
            sig[:r] *= ramp
            sig[-r:] *= ramp[::-1]
        return sig

    def _silence(self, seconds):
        n = int(round(seconds * self.sample_rate))
        return np.zeros(max(0, n), dtype=np.float64)

    def _keyed(self, text, element_fn):
        """Shared element sequencer. `element_fn(seconds)` renders one key-down
        (a tone for audio, or a keying envelope for TX). Gaps are silence."""
        dot, dash, intra, char_gap, word_gap = self._timing()
        out = []
        text = text.upper().strip()
        words = text.split(' ')
        for wi, word in enumerate(words):
            if wi > 0:
                out.append(self._silence(word_gap))
            for ci, ch in enumerate(word):
                code = TEXT_TO_MORSE.get(ch)
                if not code:
                    continue
                if ci > 0:
                    out.append(self._silence(char_gap))
                for ei, el in enumerate(code):
                    if ei > 0:
                        out.append(self._silence(intra))
                    out.append(element_fn(dot if el == '.' else dash))
        if not out:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(out).astype(np.float32)

    def encode(self, text):
        """Return a float64 audio waveform (mono) of the text as CW — an audible
        tone at `pitch`. Use for the SIDETONE (what the operator hears)."""
        return self._keyed(text, self._tone)

    def envelope(self, text):
        """Return a real 0..1 KEYING ENVELOPE for the text (no carrier tone):
        1.0 during elements with raised-cosine edges, 0.0 in gaps. This drives
        the TX modulator's CW mode so the emitted carrier sits EXACTLY on the
        dial frequency (no sidetone offset). Same timing/Farnsworth as encode()."""
        return self._keyed(text, self._env_pulse)

    def _env_pulse(self, seconds):
        """A flat-topped keying pulse (amplitude 1.0) with raised-cosine on/off
        ramps — the click-free CW envelope, carrier-free."""
        n = int(round(seconds * self.sample_rate))
        if n <= 0:
            return np.zeros(0, dtype=np.float64)
        env = np.ones(n, dtype=np.float64)
        r = int(self.sample_rate * self.rise_ms / 1000.0)
        if r > 0 and 2 * r < n:
            ramp = 0.5 * (1 - np.cos(np.pi * np.arange(r) / r))
            env[:r] = ramp
            env[-r:] = ramp[::-1]
        return env


class CWDecoder:
    def __init__(self, sample_rate=48000, pitch=600.0, wpm=20.0):
        self.sample_rate = float(sample_rate)
        self.pitch = float(pitch)
        # Adaptive dot length (seconds); seeded from an initial WPM guess.
        self.dot_s = wpm_to_dot_seconds(wpm)

        # Tone bandpass (around pitch) to isolate the CW note before detection.
        nyq = self.sample_rate / 2
        lo = max((pitch - 150) / nyq, 1e-3)
        hi = min((pitch + 150) / nyq, 0.999)
        self._sos = signal.butter(4, [lo, hi], btype='band', output='sos')
        self._zi = signal.sosfilt_zi(self._sos)

        # Envelope smoothing (one-pole), ~3 ms
        self._env_alpha = 1.0 - np.exp(-1.0 / (self.sample_rate * 0.003))
        self._env = 0.0

        # Adaptive thresholds (noise floor / signal peak trackers)
        self._floor = 1e-6
        self._peak = 1e-3

        # Key-state machine
        self._key_down = False
        self._run_samples = 0          # length of current key-down/up run
        self._symbols = []             # dots/dashes of the current character
        self._text = []                # decoded output buffer
        self._pending_space = False

    # -- envelope ----------------------------------------------------------
    def _envelope(self, audio):
        band, self._zi = signal.sosfilt(self._sos, audio, zi=self._zi)
        rect = np.abs(band)
        # one-pole smoothing, stateful
        env = np.empty(len(rect))
        e = self._env
        a = self._env_alpha
        for i, x in enumerate(rect):
            e += a * (x - e)
            env[i] = e
        self._env = e
        return env

    def _emit_char(self):
        if not self._symbols:
            return
        code = ''.join(self._symbols)
        ch = MORSE.get(code, '')
        if ch:
            self._text.append(ch)
        self._symbols = []

    def _classify_down(self, dur_s):
        """Key-down run -> dot or dash, adapting the dot length estimate."""
        # dash ~3x dot; split at 2x. Adapt dot_s toward observed elements.
        if dur_s < 2.0 * self.dot_s:
            self._symbols.append('.')
            # a dot is ~1 dot_s; nudge estimate
            self.dot_s = 0.9 * self.dot_s + 0.1 * dur_s
        else:
            self._symbols.append('-')
            self.dot_s = 0.9 * self.dot_s + 0.1 * (dur_s / 3.0)

    def _classify_up(self, dur_s):
        """Key-up run -> element gap / char gap / word gap."""
        if dur_s < 2.0 * self.dot_s:
            return  # intra-character gap; keep building the symbol
        # char gap (>=3 dots) -> finish character
        self._emit_char()
        if dur_s >= 5.0 * self.dot_s:
            # word gap (>=7 dots) -> space
            if self._text and self._text[-1] != ' ':
                self._text.append(' ')

    def process(self, audio):
        """Feed a block of demodulated CW audio; returns newly decoded text."""
        audio = np.asarray(audio, dtype=np.float64)
        if len(audio) == 0:
            return ''
        env = self._envelope(audio)

        # update floor/peak trackers from this block
        self._floor = 0.99 * self._floor + 0.01 * np.percentile(env, 20)
        self._peak = max(0.99 * self._peak + 0.01 * np.percentile(env, 95),
                         self._floor * 2)
        # hysteresis thresholds between floor and peak
        thr_hi = self._floor + 0.6 * (self._peak - self._floor)
        thr_lo = self._floor + 0.4 * (self._peak - self._floor)

        start_len = len(self._text)
        dt = 1.0 / self.sample_rate
        for x in env:
            down = self._key_down
            if down:
                if x < thr_lo:
                    # transition to key-up: classify the down run
                    self._classify_down(self._run_samples * dt)
                    self._key_down = False
                    self._run_samples = 1
                else:
                    self._run_samples += 1
            else:
                if x > thr_hi:
                    # transition to key-down: classify the up run
                    self._classify_up(self._run_samples * dt)
                    self._key_down = True
                    self._run_samples = 1
                else:
                    self._run_samples += 1
        return ''.join(self._text[start_len:])

    def flush(self):
        """Finalize any pending element/character (call at end of stream).
        Returns only the newly-emitted text (consistent with process())."""
        start = len(self._text)
        # If the stream ended mid-key-down, that run is a real final element
        # that never got an up-transition to classify it — do it now.
        if self._key_down and self._run_samples > 0:
            self._classify_down(self._run_samples / self.sample_rate)
            self._key_down = False
            self._run_samples = 0
        self._emit_char()
        return ''.join(self._text[start:])

    @property
    def text(self):
        return ''.join(self._text)

    @property
    def wpm(self):
        return 1.2 / self.dot_s if self.dot_s > 0 else 0.0
