"""Small, bounded HTTP/1.1 reader for the local desktop API."""

import asyncio
import json
from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import parse_qs, urlsplit

MAX_BODY = 1024 * 1024
MAX_HEADERS = 16 * 1024


class HTTPError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


@dataclass
class Request:
    method: str
    path: str
    query: dict
    headers: dict
    body: bytes

    def json(self):
        try:
            value = json.loads(self.body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPError(400, "无效 JSON") from exc
        if not isinstance(value, dict):
            raise HTTPError(400, "JSON 请求体必须是对象")
        return value


async def read_request(reader):
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        if len(raw) > MAX_HEADERS:
            raise HTTPError(431, "请求头过大")
        lines = raw.decode("iso-8859-1").split("\r\n")
        method, target, version = lines[0].split(" ")
        if version not in ("HTTP/1.0", "HTTP/1.1") or not target.startswith("/"):
            raise HTTPError(400, "无效 HTTP 请求")
        headers = {}
        for line in lines[1:-2]:
            key, value = line.split(":", 1)
            key = key.strip().lower()
            if not key or key in headers:
                raise HTTPError(400, "无效或重复请求头")
            headers[key] = value.strip()
        if "transfer-encoding" in headers:
            raise HTTPError(501, "不支持 Transfer-Encoding")
        length_text = headers.get("content-length", "0")
        if not length_text.isascii() or not length_text.isdecimal():
            raise HTTPError(400, "无效 Content-Length")
        length = int(length_text)
        if length > MAX_BODY:
            raise HTTPError(413, "请求体过大")
        body = await asyncio.wait_for(reader.readexactly(length), 10)
        parsed = urlsplit(target)
        return Request(method, parsed.path, parse_qs(parsed.query), headers, body)
    except asyncio.TimeoutError as exc:
        raise HTTPError(408, "读取请求超时") from exc
    except asyncio.LimitOverrunError as exc:
        raise HTTPError(431, "请求头过大") from exc
    except (ValueError, asyncio.IncompleteReadError) as exc:
        raise HTTPError(400, "不完整或无效的 HTTP 请求") from exc


def response(data, status=200, origin=None):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = (
        f"HTTP/1.1 {status} {HTTPStatus(status).phrase}\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "Cache-Control: no-store\r\n"
    )
    if origin:
        headers += f"Access-Control-Allow-Origin: {origin}\r\nVary: Origin\r\n"
    return (headers + "\r\n").encode("ascii") + body


def sse_headers(origin=None):
    headers = (
        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
        "Cache-Control: no-cache\r\nConnection: close\r\n"
        "X-Accel-Buffering: no\r\n"
    )
    if origin:
        headers += f"Access-Control-Allow-Origin: {origin}\r\nVary: Origin\r\n"
    return (headers + "\r\n").encode("ascii")


def sse_event(kind, data):
    return (f"event: {kind}\ndata: " + json.dumps(data, ensure_ascii=False, default=str)
            + "\n\n").encode("utf-8")
