#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简 CDP 客户端：连上 Electron 调试端口，执行一段 JS 并打印结果。

用法：
    python tools/cdp_eval.py "JSON.stringify(Object.keys(localStorage))"

只依赖标准库，自己实现 WebSocket 握手和帧解析，因为这个环境装不了三方包。
"""

import base64
import json
import os
import socket
import struct
import sys
import urllib.request


def read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接被关闭")
        buf += chunk
    return buf


class WS:
    def __init__(self, url, timeout=15):
        assert url.startswith("ws://"), url
        hostport, _, path = url[5:].partition("/")
        host, _, port = hostport.partition(":")
        port = int(port or 80)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock = socket.create_connection((host, port), timeout=timeout)
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("WebSocket 握手失败: " + head.decode("latin1", "replace"))

    def _send_frame(self, opcode, payload: bytes):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send(self, text: str):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv(self) -> str:
        chunks = []
        while True:
            b0, b1 = read_exact(self.sock, 2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", read_exact(self.sock, 2))[0]
            elif length == 127:
                length = struct.unpack(">Q", read_exact(self.sock, 8))[0]
            mask = read_exact(self.sock, 4) if masked else None
            data = read_exact(self.sock, length)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise ConnectionError("服务端关闭连接")
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode in (0x1, 0x2, 0x0):
                chunks.append(data)
                if fin:
                    return b"".join(chunks).decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def pick_page():
    with urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=5) as resp:
        targets = json.loads(resp.read().decode("utf-8"))
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        raise RuntimeError("没有找到可调试的页面")
    return pages[0]


def evaluate(expr: str, timeout: float = 20.0):
    page = pick_page()
    ws = WS(page["webSocketDebuggerUrl"])
    try:
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": True,
                "timeout": int(timeout * 1000),
            },
        }))
        while True:
            raw = ws.recv()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == 1:
                return msg
    finally:
        ws.close()


def main(argv):
    if len(argv) < 2:
        print("用法: python cdp_eval.py \"<js 表达式>\"", file=sys.stderr)
        return 2
    msg = evaluate(argv[1])
    if "error" in msg:
        print(json.dumps(msg["error"], ensure_ascii=False, indent=2))
        return 1
    result = msg.get("result", {})
    if "exceptionDetails" in result:
        print(json.dumps(result["exceptionDetails"], ensure_ascii=False, indent=2))
        return 1
    value = result.get("result", {})
    if "value" in value:
        print(value["value"] if isinstance(value["value"], str) else json.dumps(value["value"], ensure_ascii=False))
    else:
        print(json.dumps(value, ensure_ascii=False)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
