"""Real Sidecar + ChatEngine + OpenAI SDK against a local deterministic provider."""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Provider(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    requests = []
    def log_message(self, *args): pass
    def do_GET(self):
        if self.path == '/v1/models':
            payload = {'data':[{'id':'test-model'}]}
        elif self.path == '/v1/user/balance':
            payload = {'is_available':True,'balance_infos':[{'currency':'CNY','total_balance':'12.34'}]}
        else:
            self.send_error(404); return
        body = json.dumps(payload).encode()
        self.send_response(200); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        Provider.requests.append(body)
        self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.send_header('Connection','close'); self.end_headers()
        def emit(delta=None, usage=None, finish=None):
            chunk = {'id':'test','object':'chat.completion.chunk','created':0,'model':'test-model',
                     'choices':[] if usage else [{'index':0,'delta':delta or {},'finish_reason':finish}]}
            if usage: chunk['usage'] = usage
            self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode()); self.wfile.flush()
        latest = body['messages'][-1]
        if latest['role'] == 'user' and latest.get('content') == 'native-tool-test':
            emit({'reasoning_content':'Use a native file tool.'})
            emit({'tool_calls':[{'index':0,'id':'call_native','type':'function',
                'function':{'name':'create_file','arguments':'{"filename":"hello.py",'}}]})
            emit({'tool_calls':[{'index':0,'function':{'arguments':'"content":"print(42)\\n"}'}}]},finish='tool_calls')
        else:
            emit({'reasoning_content':'Test reasoning.'})
            emit({'content':'你好\n```python\n'})
            emit({'content':'print(42)\n```'})
            emit(finish='stop')
        emit(usage={'prompt_tokens':30,'completion_tokens':10,'total_tokens':40,
                    'prompt_cache_hit_tokens':10,'prompt_cache_miss_tokens':20})
        self.wfile.write(b'data: [DONE]\n\n'); self.wfile.flush(); self.close_connection=True


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.provider = ThreadingHTTPServer(('127.0.0.1',0),Provider)
        cls.provider_thread = threading.Thread(target=cls.provider.serve_forever,daemon=True); cls.provider_thread.start()
        with socket.socket() as s:
            s.bind(('127.0.0.1',0)); cls.port = s.getsockname()[1]
        cls.log = open(Path(cls.temp.name)/'sidecar.log','w+')
        env = dict(os.environ, DSCODE_DATA_DIR=cls.temp.name, DSCODE_SIDECAR_TOKEN='integration-token',
                   PYTHONUNBUFFERED='1', NO_PROXY='127.0.0.1,localhost')
        cls.process = subprocess.Popen([sys.executable,'sidecar/sidecar.py','--port',str(cls.port)],cwd=ROOT,env=env,stdout=cls.log,stderr=cls.log)
        for _ in range(100):
            try:
                if cls.api('GET','/api/health')[0] == 200: break
            except OSError: time.sleep(.05)
        else:
            cls.log.seek(0); raise RuntimeError(cls.log.read())
        cls.api('PUT','/api/config',{'base_url':f'http://127.0.0.1:{cls.provider.server_port}/v1',
                'api_key':'test-integration-key','model':'test-model','onboarding_completed':True,'permission_mode':'full'})
    @classmethod
    def tearDownClass(cls):
        cls.process.terminate(); cls.process.wait(8); cls.log.close()
        cls.provider.shutdown(); cls.provider.server_close(); cls.provider_thread.join(2); cls.temp.cleanup()
    @classmethod
    def api(cls,method,path,body=None):
        conn=http.client.HTTPConnection('127.0.0.1',cls.port,timeout=15)
        headers={'Authorization':'Bearer integration-token','Content-Type':'application/json'}
        conn.request(method,path,json.dumps(body) if body is not None else None,headers)
        r=conn.getresponse(); payload=json.loads(r.read()); code=r.status;conn.close();return code,payload
    def stream(self,project,text,attachments=None):
        status,run=self.api('POST','/api/chat/send',{'project':project,'text':text,'attachments':attachments or []}); self.assertEqual(status,200,run)
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=15)
        conn.request('GET','/api/chat/stream?run_id='+run['run_id'],headers={'Authorization':'Bearer integration-token'})
        response=conn.getresponse(); self.assertEqual(response.status,200)
        events=[];kind=''
        while True:
            line=response.readline().decode().strip()
            if not line:
                if response.isclosed(): break
                continue
            if line.startswith('event: '): kind=line[7:]
            if line.startswith('data: '):
                data=json.loads(line[6:]); events.append((kind,data))
                if kind in {'done','srv_error'}: break
        conn.close()
        self.assertEqual(events[-1][0],'done',events)
        return events
    def test_models_and_balance_use_custom_port_and_path(self):
        status,data=self.api('GET','/api/models');self.assertEqual(status,200);self.assertEqual(data['models'][0]['id'],'test-model')
        _,data=self.api('GET','/api/balance');self.assertEqual(data['available_balance'],12.34)
    def test_chat_history_clear_and_canonical_project(self):
        events=self.stream('chat-with-space','hello')
        self.assertEqual(''.join(d for k,d in events if k=='content'),'你好\n```python\nprint(42)\n```')
        _,history=self.api('GET','/api/projects/chat_with_space/history')
        self.assertTrue(any(m['role']=='assistant' and 'print(42)' in m['content'] for m in history['messages']))
        self.stream('chat-with-space','/clear')
        _,history=self.api('GET','/api/projects/chat_with_space/history');self.assertEqual(history['messages'],[])
        status,_=self.api('DELETE','/api/projects/chat_with_space');self.assertEqual(status,200)
    def test_native_tools_execute_and_return_matching_tool_id(self):
        events=self.stream('native','native-tool-test')
        self.assertTrue(any(k=='tool_call' for k,_ in events),events)
        self.assertEqual((Path(self.temp.name)/'projects/native/work/hello.py').read_text(),'print(42)\n')
        followup=next(b for b in reversed(Provider.requests) if any(m['role']=='tool' for m in b['messages']))
        tool=next(m for m in followup['messages'] if m['role']=='tool')
        self.assertEqual(tool['tool_call_id'],'call_native')
        assistant=next(m for m in followup['messages'] if m.get('tool_calls'))
        self.assertEqual(set(assistant['tool_calls'][0]),{'id','type','function'})
        self.assertTrue(all('id' not in m for m in followup['messages']))
        _,history=self.api('GET','/api/projects/native/history')
        self.assertEqual(history['messages'][-1]['role'],'assistant')
    def test_plan_confirmation_and_normal_mode(self):
        self.api('PUT','/api/config',{'chat_mode':'plan'})
        events=self.stream('planned','write a plan')
        self.assertTrue(any(k=='plan_ready' for k,_ in events))
        status,run=self.api('POST','/api/chat/plan',{'project':'planned','confirmed':True})
        self.assertEqual(status,200,run)
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=15)
        conn.request('GET','/api/chat/stream?run_id='+run['run_id'],headers={'Authorization':'Bearer integration-token'})
        response=conn.getresponse();data=response.read().decode();conn.close()
        self.assertIn('event: done',data)
        self.assertNotIn('event: plan_ready',data)
        self.assertNotIn('event: srv_error',data)
        self.stream('planned','/normal')
        self.assertEqual(self.api('GET','/api/config')[1]['chat_mode'],'normal')
        self.assertEqual(self.api('POST','/api/chat/stop')[0],200)
    def test_methods_and_traversal_are_rejected(self):
        self.assertEqual(self.api('GET','/api/chat/send')[0],405)
        self.assertEqual(self.api('POST','/api/projects',{'name':'../escape'})[0],400)

    def test_text_and_image_attachments_reach_real_sdk_requests(self):
        import base64
        before = len(Provider.requests)
        text = base64.b64encode('附件内容进入模型'.encode()).decode()
        status, note = self.api('POST','/api/projects/attachments/uploads',{'name':'note.txt','data':text})
        self.assertEqual(status,200,note)
        # A real, tiny PNG; the SDK transmits it as an image_url part.
        png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+yh9sAAAAASUVORK5CYII='
        status, picture = self.api('POST','/api/projects/attachments/uploads',{'name':'pixel.png','data':png})
        self.assertEqual(status,200,picture)
        self.stream('attachments','analyze attachments',[note['name'],picture['name']])
        request = Provider.requests[before]
        parts = request['messages'][-1]['content']
        self.assertTrue(any(p.get('type')=='image_url' and p['image_url']['url'].endswith(png) for p in parts))
        self.assertTrue(any('附件内容进入模型' in p.get('text','') for p in parts))
        self.assertNotIn('thinking',request)
        self.assertNotIn('reasoning_effort',request)
        self.assertTrue(all('display_content' not in m for m in request['messages']))
        history = self.api('GET','/api/projects/attachments/history')[1]['messages']
        user = next(m for m in history if m['role'] == 'user')
        self.assertEqual(user['display_content'], 'analyze attachments')
        self.assertIn('附件内容进入模型', user['content'])
