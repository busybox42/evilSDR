const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronWindow', {
  minimize: () => ipcRenderer.send('window:minimize'),
  maximizeToggle: () => ipcRenderer.send('window:maximize-toggle'),
  close: () => ipcRenderer.send('window:close'),
  onMaximized: (cb) => ipcRenderer.on('window:maximized', () => cb(true)),
  onUnmaximized: (cb) => ipcRenderer.on('window:unmaximized', () => cb(false)),
});
