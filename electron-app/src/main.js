const { app, BrowserWindow, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const net = require('net');

const SERVER_PORT = 5555;
const SERVER_HOST = '127.0.0.1';
const BACKEND_DIR = app.isPackaged
  ? path.join(process.resourcesPath, 'evilSDR')
  : path.join(__dirname, '..', '..');
const BACKEND_ENTRY = path.join(BACKEND_DIR, 'src', 'backend', 'server.py');
const DEV_VENV_PYTHON = path.join(BACKEND_DIR, '.venv', 'bin', 'python');
const BUNDLED_VENV_PYTHON = path.join(BACKEND_DIR, 'src', 'backend', 'venv', 'bin', 'python');
const DATA_ROOT = path.join(app.getPath('userData'), 'evilSDR');

let mainWindow = null;
let backendProcess = null;

function ensureDataFiles() {
  fs.mkdirSync(DATA_ROOT, { recursive: true });
  const copies = [
    'config.json',
    'bookmarks.json',
    'connections.json',
    'metadata_prefs.json'
  ];
  copies.forEach(file => {
    const target = path.join(DATA_ROOT, file);
    if (!fs.existsSync(target)) {
      const preferred = path.join(BACKEND_DIR, 'src', 'backend', file);
      const fallback = path.join(BACKEND_DIR, 'src', 'backend', `${file}.example`);
      const src = fs.existsSync(preferred) ? preferred : fallback;
      if (fs.existsSync(src)) {
        fs.copyFileSync(src, target);
      }
    }
  });
  fs.mkdirSync(path.join(DATA_ROOT, 'recordings'), { recursive: true });
}

function waitForServer(timeoutMs = 10000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    (function check() {
      const socket = net.createConnection(SERVER_PORT, SERVER_HOST, () => {
        socket.destroy();
        resolve();
      });
      socket.on('error', () => {
        socket.destroy();
        if (Date.now() - start >= timeoutMs) {
          reject(new Error('timeout waiting for backend'));
        } else {
          setTimeout(check, 200);
        }
      });
    })();
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false
    },
    title: 'evilSDR',
    autoHideMenuBar: true
  });
  mainWindow.loadURL(`http://${SERVER_HOST}:${SERVER_PORT}`);
  mainWindow.on('closed', () => {
    mainWindow = null;
    if (backendProcess) {
      backendProcess.kill();
      backendProcess = null;
    }
  });
}

function resolvePython() {
  if (process.env.PYTHON) return process.env.PYTHON;
  if (!app.isPackaged && fs.existsSync(DEV_VENV_PYTHON)) return DEV_VENV_PYTHON;
  if (app.isPackaged && fs.existsSync(BUNDLED_VENV_PYTHON)) return BUNDLED_VENV_PYTHON;
  return 'python3';
}

function startBackend() {
  ensureDataFiles();
  const python = resolvePython();
  backendProcess = spawn(python, [BACKEND_ENTRY], {
    env: {
      ...process.env,
      PYTHONUNBUFFERED: '1',
      EVILSDR_CONFIG_FILE: path.join(DATA_ROOT, 'config.json'),
      EVILSDR_BOOKMARKS_FILE: path.join(DATA_ROOT, 'bookmarks.json'),
      EVILSDR_CONNECTIONS_FILE: path.join(DATA_ROOT, 'connections.json'),
      EVILSDR_METADATA_PREFS_FILE: path.join(DATA_ROOT, 'metadata_prefs.json'),
      EVILSDR_RECORDINGS_DIR: path.join(DATA_ROOT, 'recordings')
    },
    cwd: BACKEND_DIR,
    stdio: ['ignore', 'pipe', 'pipe']
  });

  backendProcess.stdout.on('data', chunk => process.stdout.write(`[backend STDOUT] ${chunk}`));
  backendProcess.stderr.on('data', chunk => process.stderr.write(`[backend STDERR] ${chunk}`));
  backendProcess.on('exit', code => {
    console.log(`backend exited (${code})`);
    if (!app.isQuitting && code !== 0) {
      dialog.showErrorBox('evilSDR', `Backend terminated unexpectedly (code ${code}).`);
      app.quit();
    }
  });

  return waitForServer();
}

app.whenReady().then(() => {
  startBackend()
    .then(() => createWindow())
    .catch(err => {
      dialog.showErrorBox('evilSDR', `Failed to start backend: ${err.message}`);
      app.quit();
    });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('before-quit', () => {
  app.isQuitting = true;
  if (backendProcess) {
    backendProcess.kill();
  }
});
