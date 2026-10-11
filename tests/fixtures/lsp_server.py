"""Minimal framed LSP peer for transport/lifecycle contract tests."""
import json
import sys


def send(message):
    raw = json.dumps(message).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw)
    sys.stdout.buffer.flush()


while True:
    length = None
    while line := sys.stdin.buffer.readline():
        if line == b"\r\n":
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    if length is None:
        break
    request = json.loads(sys.stdin.buffer.read(length))
    method = request.get("method")
    if method == "exit":
        break
    if "id" not in request:
        continue
    result = {"capabilities": {"hoverProvider": True}} if method == "initialize" else (
        {"contents": {"kind": "plaintext", "value": "symbol documentation"}} if method == "textDocument/hover" else None)
    send({"jsonrpc": "2.0", "id": request["id"], "result": result})
