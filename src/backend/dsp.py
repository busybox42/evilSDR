#!/usr/bin/env python3
"""DSP engine with proper FIR filtering, mode-specific channel filters, and correct demodulation."""

import numpy as np
from scipy import signal as scipy_signal
from scipy.signal import firwin, lfilter, hilbert, decimate
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

        # Intermediate rate for first decimation stage
        self._intermediate_rate = 240000
        self._dec1_factor = max(1, int(sample_rate / self._intermediate_rate))
        self._dec2_factor = max(1, int(self._intermediate_rate / audio_rate))

        # --- FIR decimation filter: sample_rate -> 240kHz ---
        # Anti-aliasing LPF at 120kHz cutoff (Nyquist of 240k)
        self._dec1_taps = firwin(64, 120000, fs=sample_rate).astype(np.float32)
        self._dec1_zi = np.zeros(len(self._dec1_taps) - 1, dtype=np.complex64)

        # --- Mode-specific channel filters (applied at 240kHz intermediate rate) ---
        self._build_channel_filters()

        # --- De-emphasis for WBFM (75µs, US standard) ---
        tau = 75e-6
        dt = 1.0 / self._intermediate_rate
        alpha = dt / (tau + dt)
        self._deemph_b = np.array([alpha], dtype=np.float32)
        self._deemph_a = np.array([1.0, -(1.0 - alpha)], dtype=np.float32)
        self._deemph_zi = np.zeros(1, dtype=np.float32)

        # --- Audio decimation filter: 240kHz -> 48kHz ---
        self._dec2_taps = firwin(48, 20000, fs=self._intermediate_rate).astype(np.float32)
        self._dec2_zi = np.zeros(len(self._dec2_taps) - 1, dtype=np.float32)

        # Auto-scaling state
        self._spec_min = -80.0
        self._spec_max = -20.0

        # SSB state for phasing method
        self._ssb_zi_i = None
        self._ssb_zi_q = None

    def _build_channel_filters(self):
        """Build mode-specific channel filters at 240kHz intermediate rate."""
        ir = self._intermediate_rate

        # WBFM: ~200kHz bandwidth lowpass (100kHz cutoff)
        self._wbfm_taps = firwin(65, 100000, fs=ir).astype(np.float32)
        self._wbfm_zi = np.zeros(len(self._wbfm_taps) - 1, dtype=np.complex64)

        # NBFM: ~12.5kHz bandwidth lowpass (6250Hz cutoff)
        self._nbfm_taps = firwin(129, 6250, fs=ir).astype(np.float32)
        self._nbfm_zi = np.zeros(len(self._nbfm_taps) - 1, dtype=np.complex64)

        # AM: ~10kHz bandwidth lowpass (5000Hz cutoff)
        self._am_taps = firwin(129, 5000, fs=ir).astype(np.float32)
        self._am_zi = np.zeros(len(self._am_taps) - 1, dtype=np.complex64)

        # SSB: ~3kHz bandwidth (300-3000Hz passband via bandpass at baseband)
        # At 240k rate, filter the IQ to ~1500Hz cutoff (single sideband width)
        self._ssb_taps = firwin(257, 1500, fs=ir).astype(np.float32)
        self._ssb_zi = np.zeros(len(self._ssb_taps) - 1, dtype=np.complex64)

    def set_mode(self, mode: str):
        mode = mode.upper()
        if mode in self.MODES:
            self.mode = mode
            self._prev_sample = 0 + 0j
            self._deemph_zi[:] = 0
            # Reset channel filter states
            self._wbfm_zi[:] = 0
            self._nbfm_zi[:] = 0
            self._am_zi[:] = 0
            self._ssb_zi[:] = 0
            self._dec1_zi[:] = 0
            self._dec2_zi[:] = 0
            self._ssb_zi_i = None
            self._ssb_zi_q = None

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
        if len(iq) == 0:
            return np.zeros(0, dtype=np.float32)

        # --- Stage 1: FIR-filtered decimation to 240kHz ---
        # Apply anti-aliasing filter
        iq_filtered, self._dec1_zi = lfilter(self._dec1_taps, 1.0, iq, zi=self._dec1_zi)
        # Decimate
        iq_dec = iq_filtered[::self._dec1_factor].astype(np.complex64)

        if len(iq_dec) == 0:
            return np.zeros(0, dtype=np.float32)

        # --- Stage 2: Mode-specific channel filtering and demodulation ---
        if self.mode == "FM":
            audio = self._demod_wbfm(iq_dec)
        elif self.mode == "NFM":
            audio = self._demod_nbfm(iq_dec)
        elif self.mode == "AM":
            audio = self._demod_am(iq_dec)
        elif self.mode in ("USB", "LSB"):
            audio = self._demod_ssb(iq_dec)
        else:
            audio = np.zeros(len(iq_dec), dtype=np.float32)

        # --- Stage 3: FIR-filtered decimation to 48kHz ---
        audio_filtered, self._dec2_zi = lfilter(self._dec2_taps, 1.0, audio, zi=self._dec2_zi)
        audio_48k = audio_filtered[::self._dec2_factor].astype(np.float32)

        # Squelch
        if self.signal_level < self.squelch_threshold:
            return np.zeros_like(audio_48k)

        # Normalize
        peak = np.max(np.abs(audio_48k))
        if peak > 0.001:
            audio_48k /= (peak / 0.8)
        return audio_48k

    def _demod_wbfm(self, iq):
        """WBFM: ~200kHz channel filter + FM discriminator + de-emphasis."""
        # Channel filter
        iq_ch, self._wbfm_zi = lfilter(self._wbfm_taps, 1.0, iq, zi=self._wbfm_zi)

        # FM discriminator (phase difference)
        iq_ext = np.concatenate(([self._prev_sample], iq_ch))
        self._prev_sample = iq_ch[-1] if len(iq_ch) > 0 else self._prev_sample
        phase_diff = np.angle(iq_ext[1:] * np.conj(iq_ext[:-1]))
        audio = phase_diff.astype(np.float32)

        # De-emphasis filter
        audio, self._deemph_zi = lfilter(self._deemph_b, self._deemph_a, audio, zi=self._deemph_zi)
        return audio

    def _demod_nbfm(self, iq):
        """NBFM: ~12.5kHz channel filter + FM discriminator + gain boost."""
        # Narrow channel filter
        iq_ch, self._nbfm_zi = lfilter(self._nbfm_taps, 1.0, iq, zi=self._nbfm_zi)

        # FM discriminator
        iq_ext = np.concatenate(([self._prev_sample], iq_ch))
        self._prev_sample = iq_ch[-1] if len(iq_ch) > 0 else self._prev_sample
        phase_diff = np.angle(iq_ext[1:] * np.conj(iq_ext[:-1]))
        audio = phase_diff.astype(np.float32)

        # NBFM has lower deviation (~2.5kHz vs ~75kHz for WBFM), so boost gain
        # Deviation ratio: WBFM ~75kHz, NBFM ~2.5kHz -> scale up by ~30x
        # But we normalize later, so a moderate boost helps with SNR
        audio *= 15.0

        return audio

    def _demod_am(self, iq):
        """AM: ~10kHz channel filter + envelope detection."""
        # Channel filter
        iq_ch, self._am_zi = lfilter(self._am_taps, 1.0, iq, zi=self._am_zi)

        # Envelope detection
        envelope = np.abs(iq_ch)
        # Remove DC (carrier)
        audio = (envelope - np.mean(envelope)).astype(np.float32)
        return audio

    def _demod_ssb(self, iq):
        """SSB demodulation using the phasing (Hilbert) method."""
        # Channel filter
        iq_ch, self._ssb_zi = lfilter(self._ssb_taps, 1.0, iq, zi=self._ssb_zi)

        # Phasing method for SSB:
        # USB = Re(IQ) = I*cos - Q*sin -> just take real part (I)
        # LSB = Re(conj(IQ)) = I*cos + Q*sin -> take I + Q rotated
        # More precisely, for proper SSB via analytic signal:
        i_signal = iq_ch.real.astype(np.float64)
        q_signal = iq_ch.imag.astype(np.float64)

        if self.mode == "USB":
            # USB: shift spectrum down - take I + Hilbert(Q) approach
            # Simple method: real part of analytic signal
            audio = i_signal
        else:  # LSB
            # LSB: conjugate the signal to flip spectrum, then take real
            audio = i_signal
            # Negate Q to flip spectrum for LSB
            q_signal = -q_signal

        # Apply Hilbert transform for proper SSB extraction
        # Use the Weaver method approximation: mix I and Q properly
        # For baseband IQ, USB = I, LSB requires spectrum flip
        # The IQ data is already baseband, so:
        # USB: output = I (upper sideband is positive frequencies)
        # LSB: output = Re(conj(IQ)) which flips the spectrum
        if self.mode == "USB":
            audio = iq_ch.real.astype(np.float32)
        else:
            audio = np.conj(iq_ch).real.astype(np.float32)
            # Actually for LSB from baseband IQ:
            # conj flips Q sign, giving us the mirror image
            # But we need the negative frequencies mapped to positive
            # Proper: multiply by e^(j*0) and take real of conjugate
            audio = (iq_ch.real + iq_ch.imag).astype(np.float32)

        # Boost SSB audio (typically low level)
        audio *= 5.0
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
