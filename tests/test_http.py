import asyncio
import json
import unittest
from types import SimpleNamespace
from sidecar.http_protocol import HTTPError, MAX_BODY, Request, read_request
from sidecar.sidecar import SidecarServer


class EchoService:
    def route(self, request):
        return request.json() if request.method == 'POST' else {'service': 'dscode-sidecar'}


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = SidecarServer(EchoService(), token='test-token')
        self.server = await asyncio.start_server(self.api.handle, '127.0.0.1', 0, limit=16384)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def request(self, head, body=b'', split=False):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        writer.write(head)
        await writer.drain()
        if split:
            await asyncio.sleep(.05)
        writer.write(body)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 2)
        writer.close()
        await writer.wait_closed()
        return data.decode()

    async def test_fragmented_large_utf8_body(self):
        body = json.dumps({'text': '中文' * 30000}, ensure_ascii=False).encode()
        head = (f'POST /api/echo HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer test-token\r\n'
                f'Content-Length: {len(body)}\r\n\r\n').encode()
        reply = await self.request(head, body, split=True)
        self.assertTrue(reply.startswith('HTTP/1.1 200 OK'))
        self.assertEqual(json.loads(reply.split('\r\n\r\n')[1])['text'], '中文' * 30000)

    async def test_oversized_body_rejected_before_read(self):
        reply = await self.request(f'POST / HTTP/1.1\r\nContent-Length: {MAX_BODY+1}\r\n\r\n'.encode())
        self.assertTrue(reply.startswith('HTTP/1.1 413 '))

    async def test_untrusted_origin_and_missing_token(self):
        for header, expected in [('Origin: https://untrusted.example\r\n', 403), ('', 401)]:
            reply = await self.request(f'GET /api/health HTTP/1.1\r\n{header}\r\n'.encode())
            self.assertTrue(reply.startswith(f'HTTP/1.1 {expected}'))
            self.assertNotIn('Access-Control-Allow-Origin: *', reply)

    async def test_electron_origin_and_preflight(self):
        reply = await self.request(b'GET /api/health HTTP/1.1\r\nOrigin: null\r\nAuthorization: Bearer test-token\r\n\r\n')
        self.assertIn('Access-Control-Allow-Origin: null', reply)
        reply = await self.request(b'OPTIONS /api/config HTTP/1.1\r\nOrigin: null\r\n\r\n')
        self.assertTrue(reply.startswith('HTTP/1.1 204'))
        self.assertIn('Authorization', reply)

    async def test_invalid_length_and_duplicate_headers(self):
        for headers in [b'Content-Length: -1', b'Content-Length: 2\r\nContent-Length: 3', b'Transfer-Encoding: chunked']:
            reply = await self.request(b'POST / HTTP/1.1\r\n' + headers + b'\r\n\r\n')
            self.assertTrue(reply.startswith(('HTTP/1.1 400', 'HTTP/1.1 501')))

    async def test_incomplete_body(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b'POST / HTTP/1.1\r\nContent-Length: 5\r\n\r\nhi')
        reader.feed_eof()
        with self.assertRaises(HTTPError) as error:
            await read_request(reader)
        self.assertEqual(error.exception.status, 400)

    def test_json_object_required(self):
        for body in [b'[]', b'null', b'1', b'{bad']:
            with self.assertRaises(HTTPError):
                Request('POST', '/', {}, {}, body).json()

class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_runtime import Engine, Projects
        from sidecar.runtime import ChatRuntime
        from sidecar.service import SidecarService
        self.engine = Engine()
        self.runtime = ChatRuntime(SimpleNamespace(get=lambda key, default:default),Projects(),self.engine)
        self.api = SidecarServer(SidecarService(self.runtime),stream_timeout=.1)
        self.server = await asyncio.start_server(self.api.handle,'127.0.0.1',0)
        self.port = self.server.sockets[0].getsockname()[1]
    async def asyncTearDown(self):
        self.runtime.stop()
        if self.runtime.worker: await asyncio.to_thread(self.runtime.worker.join,2)
        self.server.close();await self.server.wait_closed()
    async def connect(self,run):
        reader,writer=await asyncio.open_connection('127.0.0.1',self.port)
        writer.write(f'GET /api/chat/stream?run_id={run.id} HTTP/1.1\r\nHost: localhost\r\n\r\n'.encode());await writer.drain()
        header=await reader.readuntil(b'\r\n\r\n');self.assertIn(b'200 OK',header)
        return reader,writer
    async def test_stream_timeout_cancels_engine(self):
        run=self.runtime.start('demo','wait')
        reader,writer=await self.connect(run)
        data=await asyncio.wait_for(reader.read(),2)
        self.assertIn(b'event: srv_error',data)
        self.assertTrue(await asyncio.to_thread(run.finished.wait,1))
        self.assertTrue(run.cancelled)
        writer.close();await writer.wait_closed()
    async def test_disconnect_cancels_only_its_run(self):
        run=self.runtime.start('demo','wait')
        reader,writer=await self.connect(run)
        writer.close();await writer.wait_closed()
        self.assertTrue(await asyncio.to_thread(run.finished.wait,1))
        self.assertTrue(run.cancelled)
