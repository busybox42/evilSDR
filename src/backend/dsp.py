#!/usr/bin/env python3
"""Lean DSP engine - FFT + FM demod only for Phase 1."""

import numpy as np
from scipy import signal as scipy_signal
import logging

logger = logging.getLogger(__name__)


class RadioDSP:
    MODES = ("AM", "FM", "NFM", "USB", "LSB")

    def __init__(self, sample_rate=2_400_000, audio_rate=48000, fft_size=2048):
        self.sample_rate = sample_rate
        self.audio_rate = audio_rate
        self.fft_size = fft_size
        self.mode = "FM"
        self.squelch_threshold = -60.0
        self.signal_level = -100.0

        self.fft_window = np.blackman(fft_size).astype(np.float32)
        self._prev_sample = 0 + 0j

        # De-emphasis for WBFM
        self._intermediate_rate = 240000
        tau = 75e-6
        dt = 1.0 / self._intermediate_rate
        alpha = dt / (tau + dt)
        self._deemph_b = np.array([alpha], dtype=np.float32)
        self._deemph_a = np.array([1.0, -(1.0 - alpha)], dtype=np.float32)
        self._deemph_zi = np.zeros(1, dtype=np.float32)

        # Decimation factor: sample_rate -> 240k
        self._dec1_factor = max(1, int(sample_rate / self._intermediate_rate))
        # 240k -> 48k
        self._dec2_factor = 5

        # Auto-scaling state
        self._spec_min = -80.0
        self._spec_max = -20.0

    def set_mode(self, mode: str):
        mode = mode.upper()
        if mode in self.MODES:
            self.mode = mode
            self._prev_sample = 0 + 0j
            self._deemph_zi[:] = 0

    def set_squelch(self, threshold_db: float):
        self.squelch_threshold = threshold_db

    def compute_fft(self, iq: np.ndarray) -> dict:
        n = self.fft_size
        if len(iq) < n:
            iq = np.pad(iq, (0, n - len(iq)), 'constant')

        chunk = iq[-n:] * self.fft_window
        spectrum = np.fft.fftshift(np.fft.fft(chunk))
        mag_db = 20.0 * np.log10(np.abs(spectrum) + 1e-12)

        # Signal level
        dbfs_offset = 20.0 * np.log10(n)
        center = mag_db[n * 45 // 100: n * 55 // 100]
        self.signal_level = float(np.max(center)) - dbfs_offset if len(center) else -100.0

        # Auto-scale
        cur_min = float(np.percentile(mag_db, 2))
        cur_max = float(np.percentile(mag_db, 99.8)) + 10.0
        self._spec_min += (0.3 if cur_min < self._spec_min else 0.05) * (cur_min - self._spec_min)
        self._spec_max += (0.3 if cur_max > self._spec_max else 0.05) * (cur_max - self._spec_max)
        span = self._spec_max - self._spec_min
        if span < 20:
            mid = (self._spec_max + self._spec_min) / 2.0
            self._spec_min, self._spec_max = mid - 10.0, mid + 10.0

        normalized = np.clip((mag_db - self._spec_min) / (self._spec_max - self._spec_min), 0.0, 1.0)
        return {
            "magnitudes": normalized.astype(np.float32),
            "min_db": round(self._spec_min, 1),
            "max_db": round(self._spec_max, 1),
            "signal_db": round(self.signal_level, 1),
        }

    def demodulate(self, iq: np.ndarray) -> np.ndarray:
        # Decimate to 240k using boxcar
        n = (len(iq) // self._dec1_factor) * self._dec1_factor
        if n == 0:
            return np.zeros(0, dtype=np.float32)
        iq_dec = iq[:n].reshape(-1, self._dec1_factor).mean(axis=1).astype(np.complex64)

        if self.mode in ("FM", "NFM"):
            audio = self._demod_fm(iq_dec, wideband=(self.mode == "FM"))
        elif self.mode == "AM":
            envelope = np.abs(iq_dec)
            audio = (envelope - np.mean(envelope)).astype(np.float32)
        elif self.mode in ("USB", "LSB"):
            audio = (np.conj(iq_dec).real if self.mode == "LSB" else iq_dec.real).astype(np.float32)
        else:
            audio = np.zeros(len(iq_dec), dtype=np.float32)

        # Decimate to 48k
        n2 = (len(audio) // self._dec2_factor) * self._dec2_factor
        if n2 == 0:
            return np.zeros(0, dtype=np.float32)
        audio_48k = audio[:n2].reshape(-1, self._dec2_factor).mean(axis=1).astype(np.float32)

        # Squelch
        if self.signal_level < self.squelch_threshold:
            return np.zeros_like(audio_48k)

        # Normalize
        peak = np.max(np.abs(audio_48k))
        if peak > 0.001:
            audio_48k /= (peak / 0.8)
        return audio_48k

    def _demod_fm(self, iq, wideband=True):
        iq_ext = np.concatenate(([self._prev_sample], iq))
        self._prev_sample = iq[-1]
        phase_diff = np.angle(iq_ext[1:] * np.conj(iq_ext[:-1]))
        audio = phase_diff.astype(np.float32)
        if wideband:
            audio, self._deemph_zi = scipy_signal.lfilter(
                self._deemph_b, self._deemph_a, audio, zi=self._deemph_zi)
        return audio

    def get_signal_level(self) -> float:
        """Return current signal level in dBFS."""
        return self.signal_level

    @staticmethod
    def dbfs_to_s_units(dbfs: float) -> str:
        thresholds = [(-10, "S9+60"), (-16, "S9+40"), (-22, "S9+20"), (-28, "S9"),
                      (-34, "S8"), (-40, "S7"), (-46, "S6"), (-52, "S5"),
                      (-58, "S4"), (-64, "S3"), (-70, "S2"), (-76, "S1")]
        for thresh, label in thresholds:
            if dbfs > thresh:
                return label
        return "S0"
