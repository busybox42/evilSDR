#!/usr/bin/env python3
"""Frequency Scanner - Bookmark stepping, squelch-based triggering, dwell/resume timers."""

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


class Scanner:
    """Frequency scanner with bookmark stepping and squelch-based stop/resume."""

    def __init__(self, rtl_client, dsp, bookmarks_file=None):
        self.rtl = rtl_client
        self.dsp = dsp
        self.state = ScanState.IDLE
        self.scan_task = None

        # Scan parameters
        self.dwell_time = 0.10        # seconds to wait after tuning before measuring
        self.resume_delay = 2.0       # seconds to wait after signal drops before resuming
        self.squelch_threshold = -60.0 # dB threshold for signal detection

        # Bookmark scan state
        self.bookmark_freqs = []      # list of {freq, mode, label}
        self.current_index = 0
        self.skip_set = set()         # frequencies to skip this session

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
                    # Filter by category if specified
                    if category_name and cat.get("name", "").lower() != category_name.lower():
                        continue
                    for station in cat.get("stations", []):
                        self.bookmark_freqs.append({
                            "freq": station.get("frequency", 0),
                            "mode": station.get("mode", "FM"),
                            "label": station.get("label", ""),
                            "category": cat.get("name", ""),
                        })
            # Filter out invalid freqs
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

    async def start(self, category_name=None):
        """Start scanning through bookmarks, optionally filtered by category."""
        if self.state != ScanState.IDLE:
            # Stop existing scan first, then restart
            await self.stop()

        self.load_bookmark_freqs(category_name=category_name)
        if not self.bookmark_freqs:
            logger.warning("No bookmarks to scan")
            return

        self.skip_set.clear()
        self.current_index = 0
        self.state = ScanState.SCANNING
        self.scan_task = asyncio.create_task(self._scan_loop())
        logger.info("Scanner started")
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
        if not self.bookmark_freqs:
            return
        if 0 <= self.current_index < len(self.bookmark_freqs):
            current = self.bookmark_freqs[self.current_index]["freq"]
            self.skip_set.add(current)
            logger.info(f"Scanner skipping {current/1e6:.3f} MHz")
            # Force back to SCANNING so the loop breaks out of MONITORING/HOLD waits
            if self.state in (ScanState.MONITORING, ScanState.HOLD):
                self._advance_index()
                self.state = ScanState.SCANNING
                await self._notify_status()

    async def _scan_loop(self):
        """Main scanning coroutine."""
        try:
            while self.state != ScanState.IDLE:
                if not self.bookmark_freqs:
                    await asyncio.sleep(0.5)
                    continue

                # Get current bookmark
                entry = self.bookmark_freqs[self.current_index]
                freq = entry["freq"]
                mode = entry["mode"]
                label = entry["label"]

                # Skip if in skip set
                if freq in self.skip_set:
                    self._advance_index()
                    continue

                # Tune to frequency
                self.state = ScanState.SCANNING
                await self._tune(freq, mode)
                await self._notify_status()

                # Dwell: wait for PLL lock and buffer fill
                await asyncio.sleep(self.dwell_time)

                # Check signal level
                signal_db = self.dsp.get_signal_level()

                if signal_db > self.squelch_threshold:
                    # Signal detected - monitor
                    self.state = ScanState.MONITORING
                    await self._notify_status()
                    logger.info(f"Scanner monitoring {label} ({freq/1e6:.3f} MHz) @ {signal_db:.1f} dB")

                    # Wait until signal drops below squelch
                    while self.state == ScanState.MONITORING:
                        await asyncio.sleep(0.1)
                        signal_db = self.dsp.get_signal_level()
                        if signal_db < self.squelch_threshold:
                            break
                        if not self.state == ScanState.MONITORING: # Check if state changed externally
                             break

                    if self.state == ScanState.IDLE:
                        break

                    # Hold: wait resume_delay before moving on
                    if self.state == ScanState.MONITORING: # Only go to HOLD if we were monitoring
                        self.state = ScanState.HOLD
                        await self._notify_status()
                        logger.info(f"Scanner hold ({self.resume_delay:.1f}s)")

                        hold_start = time.monotonic()
                        while self.state == ScanState.HOLD:
                            await asyncio.sleep(0.1)
                            # If signal comes back during hold, re-monitor
                            signal_db = self.dsp.get_signal_level()
                            if signal_db > self.squelch_threshold:
                                self.state = ScanState.MONITORING
                                await self._notify_status()
                                # Wait for it to drop again
                                while self.state == ScanState.MONITORING:
                                    await asyncio.sleep(0.1)
                                    if self.dsp.get_signal_level() < self.squelch_threshold:
                                        break
                                    if self.state != ScanState.MONITORING:
                                        break
                                if self.state == ScanState.IDLE:
                                    break
                                self.state = ScanState.HOLD
                                hold_start = time.monotonic()
                                continue
                            if time.monotonic() - hold_start >= self.resume_delay:
                                break

                    if self.state == ScanState.IDLE:
                        break

                # Advance to next frequency
                if self.state != ScanState.IDLE:
                    self.state = ScanState.SCANNING
                    self._advance_index()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Scanner loop error: {e}", exc_info=True)
        finally:
            self.state = ScanState.IDLE
            await self._notify_status()

    def _advance_index(self):
        """Move to next bookmark index, wrapping around."""
        if self.bookmark_freqs:
            self.current_index = (self.current_index + 1) % len(self.bookmark_freqs)

    async def _tune(self, freq, mode):
        """Tune RTL to frequency and set demod mode."""
        try:
            await self.rtl.set_center_freq(freq)
            # We don't need to manually set dsp mode if server handles it, 
            # but here we might want to ensure mode is set if bookmark specifies it.
            # Assuming callback handles it or we call dsp directly.
            # But dsp is available here.
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
            entry = None
            if self.bookmark_freqs and 0 <= self.current_index < len(self.bookmark_freqs):
                entry = self.bookmark_freqs[self.current_index]
            await self._on_status_change({
                "type": "SCAN_STATUS", # Added type
                "state": self.state.value,
                "index": self.current_index,
                "total": len(self.bookmark_freqs),
                "freq": entry["freq"] if entry else 0,
                "label": entry["label"] if entry else "",
                "skipped": len(self.skip_set),
            })

    def get_status(self) -> dict:
        """Get current scanner status as dict."""
        entry = None
        if self.bookmark_freqs and 0 <= self.current_index < len(self.bookmark_freqs):
            entry = self.bookmark_freqs[self.current_index]
        return {
            "type": "SCAN_STATUS",
            "state": self.state.value,
            "index": self.current_index,
            "total": len(self.bookmark_freqs),
            "freq": entry["freq"] if entry else 0,
            "label": entry["label"] if entry else "",
            "skipped": len(self.skip_set),
            "dwell_ms": int(self.dwell_time * 1000),
            "resume_delay": self.resume_delay,
        }
