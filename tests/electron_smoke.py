"""Optional native desktop smoke. Requires installed Electron and a display/Xvfb."""
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_integration import IntegrationTests

ROOT=Path(__file__).resolve().parent.parent
IntegrationTests.setUpClass()
IntegrationTests.process.terminate()
IntegrationTests.process.wait(8)
try:
    screenshot=ROOT/'test-results/electron.png'
    screenshot.parent.mkdir(exist_ok=True)
    env=dict(os.environ,DSCODE_DATA_DIR=IntegrationTests.temp.name,
        DSCODE_SIDECAR_PORT=str(IntegrationTests.port),DSCODE_PYTHON=sys.executable,
        DSCODE_SMOKE_SCREENSHOT=str(screenshot),NO_PROXY='127.0.0.1,localhost')
    child=subprocess.Popen(['node','tests/electron-smoke.cjs'],cwd=ROOT,env=env,start_new_session=os.name=='posix')
    try:
        result=child.wait(timeout=60)
    except subprocess.TimeoutExpired:
        if os.name=='posix':os.killpg(child.pid,signal.SIGTERM)
        else:child.terminate()
        child.wait(5)
        raise
    with socket.socket() as sock:
        assert sock.connect_ex(('127.0.0.1',IntegrationTests.port)) != 0,'Electron left its Sidecar running'
    sys.exit(result)
finally:
    IntegrationTests.tearDownClass()
