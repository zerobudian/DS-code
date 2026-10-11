const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const { randomBytes } = require('node:crypto');
const { SidecarProcess } = require('./sidecar-process');

const root = path.join(__dirname, '..');
const localPython = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const port = Number(process.env.DSCODE_SIDECAR_PORT || 8765);
const token = randomBytes(32).toString('hex');
const sidecar = new SidecarProcess({
  python: process.env.DSCODE_PYTHON || (fs.existsSync(localPython) ? localPython : 'python3'),
  script: path.join(root, 'sidecar', 'sidecar.py'), cwd: root, port, token,
});
let mainWindow = null;
let canQuit = false;

function openExternal(url) {
  try {
    const parsed = new URL(url);
    if (['http:', 'https:'].includes(parsed.protocol)) shell.openExternal(url);
  } catch {}
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280, height: 860, minWidth: 900, minHeight: 600,
    title: 'DS Code', backgroundColor: '#F8F9FB',
    webPreferences: {
      nodeIntegration: false, contextIsolation: true, sandbox: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    openExternal(url);
    return { action: 'deny' };
  });
  mainWindow.webContents.on('will-navigate', (event, url) => {
    event.preventDefault(); openExternal(url);
  });
  mainWindow.loadFile(path.join(root, 'renderer', 'index.html'));
  if (process.argv.includes('--dev')) mainWindow.webContents.openDevTools();
  mainWindow.on('closed', () => { mainWindow = null; });
}

ipcMain.handle('get-sidecar-config', event => {
  if (!mainWindow || event.sender !== mainWindow.webContents) throw new Error('Unknown renderer');
  return { url: sidecar.url, token };
});

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => { mainWindow?.show(); mainWindow?.focus(); });
  app.whenReady().then(async () => {
    try {
      if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid DSCODE_SIDECAR_PORT');
      await sidecar.start();
      createWindow();
    } catch (error) {
      dialog.showErrorBox('DS Code 启动失败', error.message);
      app.quit();
    }
  });
  app.on('activate', () => { if (!mainWindow && sidecar.child) createWindow(); });
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', event => {
    if (canQuit) return;
    event.preventDefault();
    sidecar.stop().finally(() => { canQuit = true; app.quit(); });
  });
}
