const { app, BrowserWindow, ipcMain } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const net = require('net');

const SIDECAR_PORT = 8765;
let sidecarProcess = null;
let mainWindow = null;

// ── Python Sidecar 生命周期管理 ──────────────────────
function startSidecar() {
  // 先杀掉所有占用 SIDECAR_PORT 的旧进程
  try {
    const { execSync } = require('child_process');
    execSync(`lsof -ti:${SIDECAR_PORT} | xargs kill -9 2>/dev/null || true`, { timeout: 3000 });
  } catch {}

  console.log('[Electron] Starting Python sidecar...');
  const sidecarPath = path.join(__dirname, '..', 'sidecar', 'sidecar.py');
  const cwd = path.join(__dirname, '..');

  sidecarProcess = spawn('python3', [sidecarPath, '--port', String(SIDECAR_PORT)], {
    cwd: cwd,
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  sidecarProcess.stdout.on('data', (data) => {
    console.log(`[sidecar] ${data.toString().trim()}`);
  });

  sidecarProcess.stderr.on('data', (data) => {
    console.error(`[sidecar:err] ${data.toString().trim()}`);
  });

  sidecarProcess.on('close', (code) => {
    console.log(`[Electron] Sidecar exited (code=${code})`);
    sidecarProcess = null;
  });
}

function stopSidecar() {
  if (sidecarProcess) {
    console.log('[Electron] Stopping sidecar...');
    sidecarProcess.kill('SIGTERM');
    setTimeout(() => {
      if (sidecarProcess) {
        sidecarProcess.kill('SIGKILL');
      }
    }, 3000);
  }
}

function waitForSidecar(retries = 30) {
  return new Promise((resolve, reject) => {
    const check = () => {
      const socket = new net.Socket();
      socket.setTimeout(500);
      socket.connect(SIDECAR_PORT, '127.0.0.1', () => {
        socket.destroy();
        resolve();
      });
      socket.on('error', () => {
        socket.destroy();
        if (retries-- > 0) {
          setTimeout(check, 500);
        } else {
          reject(new Error('Sidecar 启动超时'));
        }
      });
    };
    check();
  });
}

// ── 主窗口 ─────────────────────────────────────────
async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: 'DS Code',
    backgroundColor: '#F8F9FB',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  // 等待 sidecar 就绪
  try {
    await waitForSidecar();
    console.log('[Electron] Sidecar ready, loading UI...');
  } catch (e) {
    console.error('[Electron] Sidecar did not start:', e.message);
  }

  // 加载前端
  mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'));

  // DevTools（开发模式）
  if (process.argv.includes('--dev')) {
    mainWindow.webContents.openDevTools();
  }

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

// ── IPC handlers ───────────────────────────────────
ipcMain.handle('get-sidecar-url', () => {
  return `http://127.0.0.1:${SIDECAR_PORT}`;
});

// ── App 生命周期 ───────────────────────────────────
app.whenReady().then(async () => {
  await createWindow();
});

app.on('before-quit', () => {
  stopSidecar();
});

app.on('window-all-closed', () => {
  app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow();
  }
});
