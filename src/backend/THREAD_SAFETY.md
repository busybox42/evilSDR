# Thread Safety Refactoring - evilSDR Server

## Summary of Changes

This refactoring addresses critical thread-safety issues in the concurrent listener/broadcaster system during high-frequency SDR streaming.

## Issues Resolved

### 1. **Client Dictionary Race Condition** ✅
**Problem:** `SDRServer.clients` dictionary was iterated in `_broadcast()` while being modified by `register()`/`unregister()`, causing potential `RuntimeError: dictionary changed size during iteration`.

**Solution:**
- Added `_clients_lock` (threading.Lock) to protect all access to the clients dictionary
- `_broadcast()` now creates a snapshot (`list(self.clients.values())`) before iteration
- `register()` and `unregister()` use the lock when modifying the dictionary
- `_client_sender()` uses the lock when reading client audio slots

### 2. **ThreadPoolExecutor Shared Resource Access** ✅
**Problem:** `_process_chunk()` runs in a `ThreadPoolExecutor` and accessed shared resources that could be modified/closed by the main event loop:
- `dsp` object (mode, squelch settings)
- `decoders` dictionary
- File handles (`iq_capture_file`, `audio_wav_file`)

**Solution:**
- Added `_dsp_lock` (threading.Lock) to protect DSP state during operations
- Added `_recording_lock` (threading.Lock) to protect recording file handles
- `_process_chunk()` now receives locks as parameters and acquires them before:
  - Calling `dsp.compute_fft()` and `dsp.demodulate()` (DSP operations)
  - Writing to IQ or audio recording files
  - Double-checks file handles after acquiring locks (TOCTOU protection)
- DSP state modifications (`SET_MODE`, `SET_SQUELCH`) now acquire `_dsp_lock`
- Recording start/stop methods use `_recording_lock` for atomic state changes

### 3. **High-Frequency Queue and Audio Slot Updates** ✅
**Problem:** Client queues and audio slots were updated without proper synchronization.

**Solution:**
- Queue operations (`put_nowait`) are inherently thread-safe in asyncio
- Audio slot access protected by `_clients_lock` (both write in `_broadcast` and read in `_client_sender`)
- Client snapshot prevents stale references during iteration

## Lock Hierarchy

To prevent deadlocks, locks are always acquired in this order when multiple locks are needed:
1. `_clients_lock`
2. `_dsp_lock`
3. `_recording_lock`

In practice, these locks are rarely (if ever) acquired together, minimizing contention.

## Performance Impact

**Minimal overhead:**
- Locks are held for very short durations (microseconds)
- Snapshot creation in `_broadcast()` is O(n) where n = number of connected clients (typically < 10)
- DSP operations are the bottleneck (~milliseconds), not lock acquisition
- Recording file writes are infrequent and buffered

## Testing Recommendations

1. **Concurrent client connections:** Connect/disconnect multiple clients rapidly during streaming
2. **Mode changes during processing:** Rapidly switch modes (AM/FM/USB/LSB) while streaming
3. **Recording stress test:** Start/stop IQ and audio recording repeatedly during high-frequency updates
4. **Scanner + decoder + recording:** Enable all features simultaneously to test lock contention
5. **Long-running stability:** Stream for extended periods (hours) to verify no deadlocks or resource leaks

## Code Locations

- **Locks defined:** `SDRServer.__init__()` (~line 74)
- **Client dictionary protection:** `register()`, `unregister()`, `_broadcast()`, `_client_sender()`
- **DSP state protection:** `_process_chunk()`, `handle_message()` (SET_MODE, SET_SQUELCH)
- **Recording protection:** `_start_iq_recording()`, `_stop_iq_recording()`, `_start_audio_recording()`, `_stop_audio_recording()`, `_process_chunk()`

## Notes

- All locks use Python's `threading.Lock` (not RLock) since re-entrant behavior is not needed
- Asyncio event loop operations remain single-threaded; locks only protect cross-thread access
- The ThreadPoolExecutor uses `max_workers=1` to serialize chunk processing, preventing decoder race conditions

---

**Refactored by:** Molly (OpenClaw Agent)  
**Date:** 2026-02-16  
**Status:** Complete ✅
