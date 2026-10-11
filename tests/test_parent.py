import http.client
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.name=='posix','Parent PID reparenting is a POSIX feature')
class ParentLifecycleTests(unittest.TestCase):
    def test_sidecar_exits_after_owning_process_dies(self):
        with tempfile.TemporaryDirectory() as data:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            helper="""
import os,subprocess,sys
child=subprocess.Popen([sys.executable,'sidecar/sidecar.py','--port',sys.argv[1]],
    env=dict(os.environ,DSCODE_PARENT_PID=str(os.getpid())),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print(child.pid,flush=True)
sys.stdin.read()
"""
            parent=subprocess.Popen([sys.executable,'-c',helper,str(port)],cwd=ROOT,
                env=dict(os.environ,DSCODE_DATA_DIR=data,DSCODE_SIDECAR_TOKEN=''),
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
            child_pid=int(parent.stdout.readline())
            def healthy():
                conn=http.client.HTTPConnection('127.0.0.1',port,timeout=.2)
                try:
                    conn.request('GET','/api/health');response=conn.getresponse();response.read();return response.status==200
                except OSError:return False
                finally:conn.close()
            try:
                for _ in range(100):
                    if healthy():break
                    time.sleep(.05)
                else:self.fail('Sidecar failed to start')
                parent.terminate();parent.wait(2)
                for _ in range(100):
                    if not healthy():break
                    time.sleep(.05)
                else:self.fail('Sidecar survived its parent')
            finally:
                if parent.poll() is None:parent.terminate();parent.wait(2)
                parent.stdin.close();parent.stdout.close()
                try:os.kill(child_pid,15)
                except ProcessLookupError:pass
