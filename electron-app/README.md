# evilSDR Electron Shell

This wrapper spins up the Python backend and embeds the existing web UI in a dedicated window so the app feels standalone.

## Setup

```bash
cd electron-app
npm install
npm start
```

`npm start` launches Electron which spawns `python3 backend/server.py`, waits for port 5555, and then opens a BrowserWindow pointed at the backend.

## Packaging

```bash
cd electron-app
npm install
npm run build
```

Then from repo root, prepare an AppDir for AppImage:

```bash
scripts/build_electron_appimage.sh
```

## Notes

If Python is not on PATH, set `PYTHON` before launch.
