const { spawn } = require('node:child_process');
const http = require('node:http');

function health(url, token) {
  return new Promise((resolve, reject) => {
    const request = http.get(`${url}/api/health`, { headers: { Authorization: `Bearer ${token}` } }, response => {
      let body = '';
      response.on('data', chunk => { body += chunk; });
      response.on('end', () => {
        try {
          const data = JSON.parse(body);
          if (response.statusCode !== 200 || data.service !== 'dscode-sidecar') throw new Error('Sidecar health check failed');
          resolve(data);
        } catch (error) { reject(error); }
      });
      response.on('error', reject);
    });
    request.setTimeout(500, () => request.destroy(new Error('Sidecar health check timed out')));
    request.on('error', reject);
  });
}

class SidecarProcess {
  constructor({ python, script, cwd, port, token, spawnProcess = spawn, checkHealth = health }) {
    Object.assign(this, { python, script, cwd, port, token, spawnProcess, checkHealth });
    this.url = `http://127.0.0.1:${port}`;
    this.child = null;
    this.stopping = null;
  }

  async start(timeout = 15000) {
    if (this.child) throw new Error('Sidecar is already running');
    const child = this.spawnProcess(this.python, [this.script, '--port', String(this.port)], {
      cwd: this.cwd, env: { ...process.env, DSCODE_SIDECAR_TOKEN: this.token,
        DSCODE_PARENT_PID: String(process.pid), PYTHONUNBUFFERED: '1' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    this.child = child;
    let failure = null;
    child.on('error', error => { failure = error; });
    child.on('close', code => {
      failure = failure || new Error(`Sidecar exited (${code}); check Python dependencies and port ${this.port}`);
      if (this.child === child) this.child = null;
    });
    child.stdout?.on('data', data => console.log(`[sidecar] ${data.toString().trim()}`));
    child.stderr?.on('data', data => console.error(`[sidecar] ${data.toString().trim()}`));
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (failure) throw failure;
      try {
        await this.checkHealth(this.url, this.token);
        if (failure) throw failure;
        return;
      } catch (error) {
        if (failure) throw failure;
        await new Promise(resolve => setTimeout(resolve, 100));
      }
    }
    throw new Error(`Sidecar startup timed out on port ${this.port}`);
  }

  stop() {
    if (this.stopping) return this.stopping;
    const child = this.child;
    if (!child) return Promise.resolve();
    this.stopping = new Promise(resolve => {
      const timer = setTimeout(() => child.kill('SIGKILL'), 3000);
      child.once('close', () => { clearTimeout(timer); resolve(); });
      child.kill('SIGTERM');
    }).finally(() => { this.stopping = null; });
    return this.stopping;
  }
}

module.exports = { SidecarProcess, health };
