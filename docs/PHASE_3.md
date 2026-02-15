# Phase 3: Advanced Features & Decoders

**Goal:** Extend functionality beyond basic audio to specialized signal decoding (ADSB, TV) and complete the scanning capabilities.

## 1. Advanced Decoders
### ADSB (Automatic Dependent Surveillance–Broadcast)
- **Status**: Planned.
- **Implementation Strategy**: **External Integration**.
  - Spawn `dump1090` subprocess.
  - Parse Beast/AVR output in Python.
  - **Frontend**: Map interface (Leaflet/Mapbox) for aircraft positions.

### TV (Analog / ATV)
- **Status**: Experimental.
- **Challenge**: Real-time PAL/NTSC demodulation is CPU intensive.
- **Approach**: Low-framerate snapshot decoder (1-5 FPS) or external tool integration (`tv-sharp`).

### POCSAG (Pager)
- **Status**: Initial implementation in `src/backend/decoders/pocsag.py`.
- **To Do**: Improve bit-slicing and error correction.

## 2. Scanning (Partial Complete)
- **Frequency Scanning**:
  - **Implemented**: Range Sweeping (Start/End/Step) in `src/backend/scanner.py`.
  - **To Do**: Database logging of hits.
- **Memory Scanning**:
  - **Implemented**: Cycling through bookmarks.

## 3. Plugin Architecture
- **Goal**: Allow dynamic loading of decoders.
- **API**: `Plugin.process(iq_samples) -> resulting_data`.
- **Status**: Not started.

## 4. Tasks
- [ ] **ADSB**: Create `src/backend/decoders/adsb_wrapper.py`.
- [ ] **TV**: Research `numpy`-based AM video demodulation.
- [x] **Scanner**: Range Sweeping implemented.
- [ ] **Scanner**: Log hits to database.
- [ ] **Frontend**: Add "Decoder" tab.
