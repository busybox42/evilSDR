# evilSDR

## Overview
A lightweight, performant SDR receiver focused on stability and audio quality.
This is a ground-up rewrite to address previous performance issues (audio skipping).

## Features
- **High Performance**: Optimized DSP pipeline using FIR filters and polyphase decimation.
- **Stable Streaming**: Minimal latency and no audio skipping (ring buffer implemented).
- **Audio & IQ Recording**:
  - Record demodulated audio to WAV.
  - Capture raw IQ data for offline analysis.
- **Scanning**:
  - **Frequency Scanning**: Sweep a range of frequencies (Start/End/Step) to find active signals.
  - **Memory Scanning**: Cycle through saved bookmarks.
- **Demodulation**:
  - **WBFM**: Wideband FM (Broadcast Radio).
  - **NBFM**: Narrowband FM (Walkie Talkies, Emergency Services).
  - **AM**: Amplitude Modulation (Air Traffic).
- **Configuration**: User-space configuration via `src/backend/config.json`.

## Structure
- `src/backend`: Python-based SDR interface and DSP pipeline.
- `src/frontend`: Web-based visualization (Waterfall/Spectrum).
- `src/shared`: Common utilities.
- `tests`: Unit and integration tests.
- `docs`: Project documentation.
- `scripts`: Utility scripts for setup/running.
- `recordings`: Directory where Audio/IQ files are saved.

## Roadmap
- **Phase 1 (Complete)**: Stable RTL-TCP connection, minimal DSP, basic waterfall/spectrum visualization.
- **Phase 2 (Complete)**: Audio demodulation (WBFM/NBFM), Audio/IQ Recording, and upgraded FIR-based DSP.
- **Phase 3 (In Progress)**: Advanced decoders (ADSB, TV/ATV), Plugin Architecture.

## Getting Started

### Prerequisites
- Python 3.9+
- RTL-SDR dongle (and `rtl_tcp` running or accessible)

### Installation
1. Clone the repository:
   ```bash
   git clone https://github.com/google-deepmind/evilSDR.git
   cd evilSDR
   ```
2. Create a virtual environment (recommended):
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

### Configuration
1. Copy the example configuration:
   ```bash
   cp src/backend/config.json.example src/backend/config.json
   ```
2. Edit `src/backend/config.json` to match your environment (e.g., `rtl_host`, `rtl_port`).

### Running
1. Start the backend server:
   ```bash
   python src/backend/server.py
   ```
2. Open the frontend in your browser:
   ```
   http://localhost:5555
   ```
