#!/usr/bin/env python3
"""Frequency Scanner - Bookmark stepping, range scanning, squelch-based triggering, non-blocking state machine."""

import asyncio
import json
import logging
import time
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class ScanState(Enum):
    IDLE = "IDLE"
    SCANNING = "SCANNING"
    MONITORING = "MONITORING"
    HOLD = "HOLD"


class ScanMode(Enum):
    BOOKMARK = "BOOKMARK"
    RANGE = "RANGE"


class Scanner:
    """Frequency scanner with bookmark stepping, range scanning, and squelch-based stop/resume.
    
    Uses a non-blocking state machine: the scan loop checks state each tick
    and transitions without blocking awaits inside nested loops.
    """

    def __init__(self, rtl_client, dsp, bookmarks_file=None):
        self.rtl = rtl_client
        self.dsp = dsp
        self.state = ScanState.IDLE
        self.scan_mode = ScanMode.BOOKMARK
        self.scan_task = None

        # Scan parameters
        self.dwell_time = 0.10        # seconds to wait after tuning before measuring
        self.resume_delay = 2.0       # seconds to wait after signal drops before resuming
        self.squelch_threshold = -60.0 # dB threshold for signal detection

        # Bookmark scan state
        self.bookmark_freqs = []      # list of {freq, mode, label}
        self.current_index = 0
        self.skip_set = set()         # frequencies to skip this session

        # Range scan state
        self.range_start = 88_000_000
        self.range_end = 108_000_000
        self.range_step = 100_000
        self.range_current = 88_000_000
        self.range_mode = "FM"        # mode to use for range scanning

        # State machine timing
        self._state_entered_at = 0.0  # monotonic time when current state was entered
        self._hold_start = 0.0

        # Bookmarks file
        self.bookmarks_file = bookmarks_file or Path(__file__).parent / "bookmarks.json"

        # Callbacks (set by server)
        self._on_freq_change = None
        self._on_mode_change = None
        self._on_status_change = None

    @property
    def is_scanning(self):
        return self.state != ScanState.IDLE

    def set_speed(self, dwell_ms: int):
        """Set dwell time in milliseconds (50-500)."""
        self.dwell_time = max(0.05, min(0.5, dwell_ms / 1000.0))
        logger.info(f"Scanner dwell time: {self.dwell_time*1000:.0f}ms")

    def set_resume_delay(self, delay_s: float):
        """Set resume delay in seconds."""
        self.resume_delay = max(0.5, min(10.0, delay_s))
        logger.info(f"Scanner resume delay: {self.resume_delay:.1f}s")

    def set_squelch(self, threshold_db: float):
        """Update squelch threshold used by scanner."""
        self.squelch_threshold = threshold_db

    def load_bookmark_freqs(self, category_name=None):
        """Load bookmark frequencies from file into scan list, optionally filtered by category."""
        self.bookmark_freqs = []
        try:
            if self.bookmarks_file.exists():
                data = json.loads(self.bookmarks_file.read_text())
                for cat in data.get("categories", []):
                    if category_name and cat.get("name", "").lower() != category_name.lower():
                        continue
                    for station in cat.get("stations", []):
                        self.bookmark_freqs.append({
                            "freq": station.get("frequency", 0),
                            "mode": station.get("mode", "FM"),
                            "label": station.get("label", ""),
                            "category": cat.get("name", ""),
                        })
            self.bookmark_freqs = [b for b in self.bookmark_freqs if b["freq"] > 0]
            cat_msg = f" (category: {category_name})" if category_name else " (all)"
            logger.info(f"Scanner loaded {len(self.bookmark_freqs)} bookmark frequencies{cat_msg}")
        except Exception as e:
            logger.error(f"Scanner failed to load bookmarks: {e}")

    def get_categories(self):
        """Return list of category names from bookmarks file."""
        try:
            if self.bookmarks_file.exists():
                data = json.loads(self.bookmarks_file.read_text())
                return [cat.get("name", "") for cat in data.get("categories", []) if cat.get("name")]
        except Exception:
            pass
        return []

    # --- Start/Stop ---

    async def start(self, category_name=None):
        """Start scanning through bookmarks, optionally filtered by category."""
        if self.state != ScanState.IDLE:
            await self.stop()

        self.scan_mode = ScanMode.BOOKMARK
        self.load_bookmark_freqs(category_name=category_name)
        if not self.bookmark_freqs:
            logger.warning("No bookmarks to scan")
            return

        self.skip_set.clear()
        self.current_index = 0
        self._transition(ScanState.SCANNING)
        self.scan_task = asyncio.create_task(self._state_machine_loop())
        logger.info("Scanner started (bookmark mode)")
        await self._notify_status()

    async def start_range(self, start_freq: int, end_freq: int, step: int, mode: str = "FM"):
        """Start range scanning from start_freq to end_freq with given step."""
        if self.state != ScanState.IDLE:
            await self.stop()

        self.scan_mode = ScanMode.RANGE
        self.range_start = start_freq
        self.range_end = end_freq
        self.range_step = max(1000, step)  # minimum 1kHz step
        self.range_current = start_freq
        self.range_mode = mode.upper()

        self.skip_set.clear()
        self._transition(ScanState.SCANNING)
        self.scan_task = asyncio.create_task(self._state_machine_loop())
        logger.info(f"Scanner started (range: {start_freq/1e6:.3f}-{end_freq/1e6:.3f} MHz, step {step/1e3:.1f}kHz, mode {mode})")
        await self._notify_status()

    async def stop(self):
        """Stop scanning."""
        self.state = ScanState.IDLE
        if self.scan_task and not self.scan_task.done():
            self.scan_task.cancel()
            try:
                await self.scan_task
            except asyncio.CancelledError:
                pass
        self.scan_task = None
        logger.info("Scanner stopped")
        await self._notify_status()

    async def skip(self):
        """Skip current frequency (add to skip set, force advance)."""
        current_freq = self._get_current_freq()
        if current_freq:
            self.skip_set.add(current_freq)
            logger.info(f"Scanner skipping {current_freq/1e6:.3f} MHz")
        if self.state in (ScanState.MONITORING, ScanState.HOLD):
            self._advance()
            self._transition(ScanState.SCANNING)
            await self._notify_status()

    # --- Non-blocking state machine ---

    def _transition(self, new_state: ScanState):
        """Transition to a new state, recording entry time."""
        self.state = new_state
        self._state_entered_at = time.monotonic()
        if new_state == ScanState.HOLD:
            self._hold_start = time.monotonic()

    async def _state_machine_loop(self):
        """Non-blocking state machine. Each tick evaluates current state and transitions."""
        TICK = 0.05  # 50ms tick
        needs_tune = True  # flag: tune when entering SCANNING with new freq

        try:
            while self.state != ScanState.IDLE:
                await asyncio.sleep(TICK)

                if self.state == ScanState.SCANNING:
                    # Skip frequencies in skip set
                    current_freq = self._get_current_freq()
                    if current_freq and current_freq in self.skip_set:
                        self._advance()
                        if self._wrapped_around():
                            break  # completed full sweep
                        needs_tune = True
                        continue

                    # Tune if needed
                    if needs_tune:
                        await self._tune_current()
                        self._state_entered_at = time.monotonic()
                        needs_tune = False
                        await self._notify_status()

                    # Wait for dwell time
                    if time.monotonic() - self._state_entered_at < self.dwell_time:
                        continue

                    # Check signal
                    signal_db = self.dsp.get_signal_level()
                    if signal_db > self.squelch_threshold:
                        # Signal found -> MONITORING
                        freq = self._get_current_freq()
                        label = self._get_current_label()
                        logger.info(f"Scanner monitoring {label} ({freq/1e6:.3f} MHz) @ {signal_db:.1f} dB")
                        self._transition(ScanState.MONITORING)
                        await self._notify_status()
                    else:
                        # No signal -> advance
                        self._advance()
                        if self._wrapped_around():
                            # For range mode, optionally loop; for bookmark, loop
                            if self.scan_mode == ScanMode.RANGE:
                                self.range_current = self.range_start
                            # Continue scanning
                        needs_tune = True

                elif self.state == ScanState.MONITORING:
                    signal_db = self.dsp.get_signal_level()
                    if signal_db < self.squelch_threshold:
                        # Signal dropped -> HOLD
                        logger.info(f"Scanner hold ({self.resume_delay:.1f}s)")
                        self._transition(ScanState.HOLD)
                        await self._notify_status()

                elif self.state == ScanState.HOLD:
                    signal_db = self.dsp.get_signal_level()
                    if signal_db > self.squelch_threshold:
                        # Signal returned -> back to MONITORING
                        self._transition(ScanState.MONITORING)
                        await self._notify_status()
                    elif time.monotonic() - self._hold_start >= self.resume_delay:
                        # Hold expired -> advance and resume scanning
                        self._advance()
                        if self.scan_mode == ScanMode.RANGE and self._wrapped_around():
                            self.range_current = self.range_start
                        self._transition(ScanState.SCANNING)
                        needs_tune = True

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Scanner loop error: {e}", exc_info=True)
        finally:
            self.state = ScanState.IDLE
            await self._notify_status()

    # --- Helpers ---

    def _get_current_freq(self) -> int:
        if self.scan_mode == ScanMode.BOOKMARK:
            if self.bookmark_freqs and 0 <= self.current_index < len(self.bookmark_freqs):
                return self.bookmark_freqs[self.current_index]["freq"]
            return 0
        else:
            return self.range_current

    def _get_current_label(self) -> str:
        if self.scan_mode == ScanMode.BOOKMARK:
            if self.bookmark_freqs and 0 <= self.current_index < len(self.bookmark_freqs):
                return self.bookmark_freqs[self.current_index].get("label", "")
        return f"{self.range_current/1e6:.3f} MHz"

    def _get_current_mode(self) -> str:
        if self.scan_mode == ScanMode.BOOKMARK:
            if self.bookmark_freqs and 0 <= self.current_index < len(self.bookmark_freqs):
                return self.bookmark_freqs[self.current_index].get("mode", "FM")
        return self.range_mode

    def _advance(self):
        """Move to next frequency."""
        if self.scan_mode == ScanMode.BOOKMARK:
            if self.bookmark_freqs:
                self.current_index = (self.current_index + 1) % len(self.bookmark_freqs)
        else:
            self.range_current += self.range_step
            if self.range_current > self.range_end:
                self.range_current = self.range_start  # wrap

    def _wrapped_around(self) -> bool:
        """Check if we've wrapped around (for range mode)."""
        if self.scan_mode == ScanMode.RANGE:
            return self.range_current > self.range_end
        return False  # bookmarks wrap via modulo

    async def _tune_current(self):
        """Tune to current frequency."""
        freq = self._get_current_freq()
        mode = self._get_current_mode()
        if not freq:
            return
        try:
            await self.rtl.set_center_freq(freq)
            self.dsp.set_mode(mode)
        except Exception:
            pass
        if self._on_freq_change:
            await self._on_freq_change(freq)
        if self._on_mode_change:
            await self._on_mode_change(mode)

    async def _notify_status(self):
        """Broadcast scanner status."""
        if self._on_status_change:
            await self._on_status_change(self.get_status())

    def get_status(self) -> dict:
        """Get current scanner status as dict."""
        base = {
            "type": "SCAN_STATUS",
            "state": self.state.value,
            "scan_mode": self.scan_mode.value,
            "skipped": len(self.skip_set),
            "dwell_ms": int(self.dwell_time * 1000),
            "resume_delay": self.resume_delay,
        }

        if self.scan_mode == ScanMode.BOOKMARK:
            entry = None
            if self.bookmark_freqs and 0 <= self.current_index < len(self.bookmark_freqs):
                entry = self.bookmark_freqs[self.current_index]
            base.update({
                "index": self.current_index,
                "total": len(self.bookmark_freqs),
                "freq": entry["freq"] if entry else 0,
                "label": entry.get("label", "") if entry else "",
            })
        else:
            total_steps = max(1, int((self.range_end - self.range_start) / self.range_step) + 1)
            current_step = int((self.range_current - self.range_start) / self.range_step)
            base.update({
                "index": current_step,
                "total": total_steps,
                "freq": self.range_current,
                "label": f"{self.range_current/1e6:.3f} MHz",
                "range_start": self.range_start,
                "range_end": self.range_end,
                "range_step": self.range_step,
            })

        return base
