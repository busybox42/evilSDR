# Phase 2: Audio Demodulation & Recording (Completed)

**Goal:** Enable audio listening (WBFM/NBFM) and data capture (Audio/IQ) without stuttering.

## 1. Audio Pipeline (Complete)
- **Demodulation**:
  - Implemented WBFM, NBFM, and AM demodulation.
  - Used FIR-based DSP chain for high quality and low CPU usage.
  - Optimized polyphase filterbanks for decimation.
- **Buffer Management**:
  - Ring buffer implemented to handle audio sample jitter.
  - Underruns/skipping eliminated.

## 2. Recording Features (Complete)
- **Audio Recording**:
  - Demodulated audio is saved to WAV files in `recordings/`.
  - Can be toggled on/off via frontend.
- **IQ Recording**:
  - Raw IQ samples captured to binary files in `recordings/`.
  - Format: Binary complex64/float32 (compatible with standard tools).
  - Dedicated thread handles disk writes to prevent blocking DSP.

## 3. Frontend Updates (Complete)
- **Audio Controls**: Volume, Mute, Squelch implemented.
- **Recording UI**: Record buttons for Audio and IQ added.
- **Mode Selection**: WBFM / NBFM / AM toggle.

## 4. Implementation Details
- **Audio**: `scipy.signal` for filtering/resampling. WebAudio API for playback.
- **Recording**: Python `wave` module for audio. Raw file I/O for IQ.
- **Threads**: Dedicated thread pool for processing and disk writes.

## 5. Completed Tasks
- [x] Implement demodulators in `src/backend/dsp.py`
- [x] Implement recording logic in `src/backend/server.py`
- [x] Frontend audio player and record buttons
