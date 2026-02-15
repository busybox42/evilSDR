# evilSDR

## Overview
A lightweight, performant SDR receiver focused on stability and audio quality.
This is a ground-up rewrite to address previous performance issues (audio skipping).

## Structure
- `src/backend`: Python-based SDR interface and DSP pipeline.
- `src/frontend`: Web-based visualization (Waterfall/Spectrum).
- `src/shared`: Common utilities and configuration.
- `tests`: Unit and integration tests.
- `docs`: Project documentation.
- `scripts`: Utility scripts for setup/running.

## Roadmap
- **Phase 1 (MVP)**: Stable RTL-TCP connection, minimal DSP, basic waterfall/spectrum visualization (no skipping).
- **Phase 2**: Audio demodulation (WBFM/NBFM) & Recording (Audio/IQ).
- **Phase 3**: Advanced decoders (ADSB, TV/ATV), scanning, and plugin architecture.

## Getting Started
1. Install dependencies: `pip install -r requirements.txt`
2. Run backend: `python src/backend/main.py`
3. Open frontend: `http://localhost:8000` (TBD)

## Team
- **Lead**: Subagent (Architect/PM)
- **Molly**: Coder (Implementation)
- **Felix**: QA (Testing & Performance)
