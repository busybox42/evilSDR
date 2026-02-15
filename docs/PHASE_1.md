# Phase 1 (MVP) Requirements

**Goal:** Stable RTL-TCP connection, minimal DSP, and basic waterfall/spectrum visualization that doesn't skip.

## 1. Backend Architecture (Python)
- **SDR Interface**: 
  - Connect to `rtl_tcp` (or native SoapySDR if local).
  - Handle buffer overflows gracefully.
  - Implement zero-copy or minimal-copy buffering where possible (NumPy).
- **DSP Pipeline**:
  - Receive I/Q samples -> Decimate (if needed) -> FFT -> Spectrum/Waterfall data.
  - Use `scipy.signal` or manual NumPy FFT.
  - **Avoid** blocking the main loop (use `asyncio` or `threading` for DSP).
- **Communication**:
  - WebSocket server for real-time spectrum data stream.
  - Minimal protocol (binary or efficient JSON).

## 2. Frontend Visualization (HTML/JS/Canvas)
- **Spectrum Display**:
  - Simple HTML5 Canvas rendering.
  - Smooth 60fps update rate if possible.
  - Basic frequency axis labeling.
- **Controls**:
  - Start/Stop stream.
  - Center Frequency adjustment (sent back to backend).
  - Gain adjustment.

## 3. Performance Criteria (QA)
- **Audio Skipping**: None allowed (though Phase 1 is visualization only, the underlying pipeline must support seamless audio later).
- **Latency**: < 200ms glass-to-glass (RF input to visual update).
- **CPU Usage**: < 50% on single core (host environment constraint).
- **Stability**: Run for > 1 hour without crashing or drifting.

## 4. Tasks
- **Molly**: Implement `src/backend/sdr_interface.py`, `src/backend/dsp.py`, and `src/frontend/index.html`.
- **Felix**: Create `tests/test_stability.py` and measure CPU/Latency.
