/**
 * evilSDR - Full Frontend (Phase 3.5/4 Restored)
 */

let ws = null;
let connected = false;
let streaming = false;
let currentFreq = 88700000;
let currentMode = 'FM';
let currentSampleRate = 2400000;

let audioCtx = null;
let audioWorklet = null;
let gainNode = null;
let audioInitialized = false;

let specCtx, watCtx;
let specCanvas, watCanvas;

let vizGain = 1.0;
let vizOffset = 0.0;
let autoScale = true;
let manualMinDb = -80;
let manualMaxDb = -20;

let lastWaterfallDraw = 0;
const WATERFALL_FPS = 25;
const WATERFALL_FPS_STREAMING = 15;

window.addEventListener('DOMContentLoaded', () => {
  specCanvas = document.getElementById('spectrum-canvas');
  watCanvas  = document.getElementById('waterfall-canvas');
  specCtx    = specCanvas.getContext('2d');
  watCtx     = watCanvas.getContext('2d');

  resize();
  window.addEventListener('resize', resize);

  wireControls();
  connect();
  loadBookmarks();
});

function resize() {
  const dpr = window.devicePixelRatio || 1;
  const w = specCanvas.clientWidth;
  if (w === 0) return;
  specCanvas.width  = w * dpr;
  specCanvas.height = 300 * dpr;
  specCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  watCanvas.width  = w * dpr;
  watCanvas.height = 400 * dpr;
}

function wireControls() {
  document.querySelectorAll('.mode-btn').forEach(btn => {
    btn.onclick = () => {
      document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentMode = btn.dataset.mode;
      sendJSON({ type: 'SET_MODE', mode: currentMode });
    };
  });

  const vol = document.getElementById('volume-slider');
  if (vol) vol.oninput = () => {
    if (gainNode) gainNode.gain.value = Math.pow(vol.value / 100, 2);
    document.getElementById('volume-value').textContent = vol.value + '%';
  };

  const sq = document.getElementById('squelch-slider');
  if (sq) sq.oninput = () => {
    sendJSON({ type: 'SET_SQUELCH', value: parseFloat(sq.value) });
    document.getElementById('squelch-value').textContent = sq.value + ' dB';
  };

  document.getElementById('rf-gain-slider').oninput = (e) => {
    sendJSON({ type: 'SET_GAIN', value: parseInt(e.target.value) });
  };

  document.getElementById('chk-agc').onchange = (e) => {
    sendJSON({ type: 'SET_AGC', value: e.target.checked });
  };

  document.getElementById('btn-bookmarks-toggle').onclick = () => {
    document.getElementById('bookmarks-panel').classList.toggle('open');
  };

  document.getElementById('btn-add-bookmark').onclick = openAddBookmarkModal;
  document.getElementById('btn-save-bookmark').onclick = saveBookmark;
  document.getElementById('btn-cancel-bookmark').onclick = () => {
    document.getElementById('bookmark-modal').style.display = 'none';
  };

  document.getElementById('btn-connect-modal').onclick = () => {
    document.getElementById('connect-modal').style.display = 'flex';
  };
  document.getElementById('btn-close-connect').onclick = () => {
    document.getElementById('connect-modal').style.display = 'none';
  };
  document.getElementById('btn-do-connect').onclick = () => {
    const host = document.getElementById('conn-host').value;
    const port = parseInt(document.getElementById('conn-port').value);
    const driver = document.getElementById('conn-driver').value;
    const sample_rate = parseInt(document.getElementById('conn-sample-rate').value);
    sendJSON({ type: 'CONNECT', host, port, driver, sample_rate });
    document.getElementById('connect-modal').style.display = 'none';
  };

  document.getElementById('btn-scan').onclick = toggleScan;
  document.getElementById('btn-skip').onclick = () => sendJSON({ type: 'SKIP_SCAN' });
  document.getElementById('chk-pocsag').onchange = (e) => {
    sendJSON({ type: 'TOGGLE_POCSAG', value: e.target.checked });
    document.getElementById('decoder-log').style.display = e.target.checked ? 'block' : 'none';
  };
}

function connect() {
  const host = location.hostname || '127.0.0.1';
  ws = new WebSocket(`ws://${host}:8765`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    document.getElementById('status-text').textContent = 'CONNECTED';
    document.getElementById('status-dot').style.background = 'var(--accent)';
    connected = true;
    sendJSON({ type: 'GET_SCAN_CATEGORIES' });
  };
  ws.onclose = () => {
    document.getElementById('status-text').textContent = 'DISCONNECTED';
    document.getElementById('status-dot').style.background = 'var(--accent-red)';
    connected = false;
    setStreamingUI(false); // Reset streaming state on disconnect
    setTimeout(connect, 2000);
  };
  ws.onmessage = (e) => {
    if (typeof e.data === 'string') handleJSON(JSON.parse(e.data));
    else handleBinary(e.data);
  };
}

function sendJSON(obj) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj)); }

function handleJSON(msg) {
  switch (msg.type) {
    case 'STATE':
      if (msg.freq) updateFreq(msg.freq);
      if (msg.mode) setModeUI(msg.mode);
      if (msg.streaming !== undefined) setStreamingUI(msg.streaming);
      break;
    case 'FREQ_CHANGED': updateFreq(msg.value); break;
    case 'MODE_CHANGED': setModeUI(msg.mode); break;
    case 'SIGNAL_LEVEL':
      const pct = Math.max(0, Math.min(100, ((msg.db + 90) / 90) * 100));
      document.getElementById('s-meter-bar').style.width = pct + '%';
      document.getElementById('s-meter-reading').textContent = msg.s_units || 'S0';
      break;
    case 'STREAM_STATE':
      setStreamingUI(msg.streaming);
      break;
    case 'SCAN_STATUS':
      updateScanStatus(msg);
      break;
    case 'SCAN_CATEGORIES':
      populateScanCategories(msg.categories);
      break;
    case 'POCSAG':
      appendDecoderLog(msg.message);
      break;
  }
}

function setStreamingUI(state) {
  streaming = state;
  const btn = document.getElementById('btn-start');
  if (btn) {
    btn.textContent = streaming ? '■ STOP' : '▶ START';
    btn.classList.toggle('active', streaming);
  }
}

function handleBinary(buf) {
  const view = new Uint8Array(buf);
  const prefix = view[0];
  // Copy to ensure alignment for Float32Array
  const alignedData = new Float32Array(buf.slice(1));
  
  if (prefix === 0x01) {
    drawSpectrum(alignedData);
    drawWaterfall(alignedData);
  } else if (prefix === 0x02) {
    if (audioWorklet && audioInitialized) {
      audioWorklet.port.postMessage(alignedData);
    }
  }
}

function drawSpectrum(data) {
  const w = specCanvas.width / (window.devicePixelRatio || 1);
  const h = 300;
  specCtx.fillStyle = '#0a0a12';
  specCtx.fillRect(0, 0, w, h);
  specCtx.beginPath();
  specCtx.strokeStyle = '#00ff88';
  specCtx.lineWidth = 1.5;
  const step = w / data.length;
  for (let i = 0; i < data.length; i++) {
    const val = data[i] * vizGain + vizOffset;
    const x = i * step;
    const y = h - val * h;
    if (i === 0) specCtx.moveTo(x, y); else specCtx.lineTo(x, y);
  }
  specCtx.stroke();
}

function drawWaterfall(data) {
  const now = performance.now();
  const fps = streaming ? WATERFALL_FPS_STREAMING : WATERFALL_FPS;
  if (now - lastWaterfallDraw < (1000 / fps)) return;
  lastWaterfallDraw = now;
  const w = watCanvas.width;
  watCtx.drawImage(watCanvas, 0, 1);
  const img = watCtx.createImageData(w, 1);
  for (let px = 0; px < w; px++) {
    const val = data[Math.floor((px / w) * data.length)] * vizGain + vizOffset;
    const idx = px * 4;
    img.data[idx] = val * 255;
    img.data[idx+1] = (1-val) * 255;
    img.data[idx+2] = 255;
    img.data[idx+3] = 255;
  }
  watCtx.putImageData(img, 0, 0);
}

async function initAudio() {
  if (audioInitialized) return;
  try {
    audioCtx = new AudioContext({ sampleRate: 48000 });
    await audioCtx.audioWorklet.addModule('./audio-processor.js?v=' + Date.now());
    audioWorklet = new AudioWorkletNode(audioCtx, 'sdr-audio-processor', { outputChannelCount: [1] });
    gainNode = audioCtx.createGain();
    audioWorklet.connect(gainNode);
    gainNode.connect(audioCtx.destination);
    audioInitialized = true;
  } catch (e) { console.error(e); }
}

function toggleStream() {
  if (!streaming) {
    initAudio().then(() => {
      if (audioCtx.state === 'suspended') audioCtx.resume();
      sendJSON({ type: 'START_STREAM' });
    });
  } else {
    // Optimistic update so UI is responsive and button isn't stuck if backend is slow/dead
    setStreamingUI(false);
    sendJSON({ type: 'STOP_STREAM' });
    if (audioWorklet) audioWorklet.port.postMessage('CLEAR');
  }
}

function setFreq() {
  const val = parseFloat(document.getElementById('freq-input').value);
  if (!isNaN(val)) sendJSON({ type: 'SET_FREQ', value: Math.round(val * 1e6) });
}

function updateFreq(hz) {
  currentFreq = hz;
  document.getElementById('freq-readout').textContent = (hz / 1e6).toFixed(3) + ' MHz';
  document.getElementById('freq-input').value = (hz / 1e6).toFixed(3);
}

function setModeUI(mode) {
  currentMode = mode;
  document.querySelectorAll('.mode-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
}

let cachedBookmarks = { categories: [] };

async function loadBookmarks() {
  try {
    const r = await fetch('/api/bookmarks');
    cachedBookmarks = await r.json();
    renderBookmarks();
  } catch (e) { console.error(e); }
}

function renderBookmarks() {
  const list = document.getElementById('bookmarks-list');
  list.innerHTML = '';
  cachedBookmarks.categories.forEach((cat, ci) => {
    const section = document.createElement('div');
    section.className = 'bm-category';
    section.innerHTML = `<div class="bm-cat-header">${cat.name}</div>`;
    (cat.stations || []).forEach((st, si) => {
      const item = document.createElement('div');
      item.className = 'bm-item';
      item.innerHTML = `<span class="bm-label">${st.label}</span><span class="bm-freq">${(st.frequency / 1e6).toFixed(3)}</span><button class="bm-delete" title="Delete">✕</button>`;
      item.querySelector('.bm-label').onclick = () => {
        sendJSON({ type: 'SET_FREQ', value: st.frequency });
        if (st.mode) sendJSON({ type: 'SET_MODE', mode: st.mode });
      };
      item.querySelector('.bm-freq').onclick = item.querySelector('.bm-label').onclick;
      item.querySelector('.bm-delete').onclick = (e) => {
        e.stopPropagation();
        deleteBookmark(ci, si);
      };
      section.appendChild(item);
    });
    list.appendChild(section);
  });
}

function openAddBookmarkModal() {
  const modal = document.getElementById('bookmark-modal');
  document.getElementById('bm-freq').value = (currentFreq / 1e6).toFixed(3);
  document.getElementById('bm-mode').value = currentMode;
  document.getElementById('bm-label').value = '';
  document.getElementById('bm-new-category').value = '';
  // Populate category dropdown
  const sel = document.getElementById('bm-category');
  sel.innerHTML = '';
  cachedBookmarks.categories.forEach((cat, i) => {
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = cat.name;
    sel.appendChild(opt);
  });
  modal.style.display = 'flex';
  document.getElementById('bm-label').focus();
}

async function saveBookmark() {
  const label = document.getElementById('bm-label').value.trim() || 'New Station';
  const freq = Math.round(parseFloat(document.getElementById('bm-freq').value) * 1e6);
  const mode = document.getElementById('bm-mode').value;
  const newCat = document.getElementById('bm-new-category').value.trim();
  const catIdx = parseInt(document.getElementById('bm-category').value);

  const entry = { label, frequency: freq, mode };

  if (newCat) {
    cachedBookmarks.categories.push({ name: newCat, stations: [entry] });
  } else if (cachedBookmarks.categories[catIdx]) {
    cachedBookmarks.categories[catIdx].stations.push(entry);
  } else {
    cachedBookmarks.categories.push({ name: 'Uncategorized', stations: [entry] });
  }

  await postBookmarks();
  document.getElementById('bookmark-modal').style.display = 'none';
}

async function deleteBookmark(catIdx, stIdx) {
  const cat = cachedBookmarks.categories[catIdx];
  if (!cat) return;
  const st = cat.stations[stIdx];
  if (!confirm(`Delete "${st.label}"?`)) return;
  cat.stations.splice(stIdx, 1);
  // Remove empty categories
  if (cat.stations.length === 0) {
    cachedBookmarks.categories.splice(catIdx, 1);
  }
  await postBookmarks();
}

async function postBookmarks() {
  try {
    await fetch('/api/bookmarks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cachedBookmarks)
    });
    renderBookmarks();
  } catch (e) { console.error('Failed to save bookmarks:', e); }
}

let isScanning = false;
function toggleScan() {
  if (isScanning) {
    sendJSON({ type: 'STOP_SCAN' });
  } else {
    const cat = document.getElementById('scan-category').value;
    const msg = { type: 'START_SCAN' };
    if (cat) msg.category = cat;
    sendJSON(msg);
  }
  // Don't toggle here — let SCAN_STATUS drive the UI state
}

function updateScanStatus(msg) {
  const btn = document.getElementById('btn-scan');
  const info = document.getElementById('scan-info');
  isScanning = msg.state !== 'IDLE';
  if (btn) btn.textContent = isScanning ? 'STOP SCAN' : 'START SCAN';
  if (info) {
    if (isScanning) {
      info.style.display = 'block';
      info.textContent = `[${msg.state}] ${msg.label || '---'} (${msg.index + 1}/${msg.total}) skip:${msg.skipped}`;
    } else {
      info.style.display = 'none';
    }
  }
}

function populateScanCategories(categories) {
  const sel = document.getElementById('scan-category');
  if (!sel) return;
  // Preserve current selection
  const cur = sel.value;
  sel.innerHTML = '<option value="">All Categories</option>';
  (categories || []).forEach(name => {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  });
  sel.value = cur; // restore if still valid
}

function appendDecoderLog(msg) {
  const log = document.getElementById('decoder-log');
  const line = document.createElement('div');
  line.textContent = `[${new Date().toLocaleTimeString()}] ${msg.address}: ${msg.content}`;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}
