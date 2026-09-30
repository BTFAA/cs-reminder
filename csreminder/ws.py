"""极简 WebSocket 客户端（只用标准库，用于连接 QQ 机器人网关）。"""
from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
import urllib.parse


class WsError(RuntimeError):
    pass


class WsClient:
    def __init__(self, url: str, timeout: float = 20.0):
        u = urllib.parse.urlparse(url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        raw = socket.create_connection((host, port), timeout=timeout)
        if u.scheme == "wss":
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(raw, server_hostname=host)
        else:
            self.sock = raw
        self.sock.settimeout(timeout)
        self.buf = b""

        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, key))
        self.sock.sendall(req.encode())

        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WsError("握手中连接被关闭")
            head += chunk
        head, self.buf = head.split(b"\r\n\r\n", 1)
        first = head.split(b"\r\n")[0].decode("latin1", "replace")
        if "101" not in first:
            raise WsError("WebSocket 握手失败: " + first[:200])

    # ---------------------------------------------------------------- 内部
    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WsError("连接已关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def settimeout(self, t):
        self.sock.settimeout(t)

    # ---------------------------------------------------------------- 收发
    def send(self, text: str):
        payload = text.encode("utf-8")
        n = len(payload)
        header = bytearray([0x81])
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

    def recv(self):
        """返回 (opcode, text)；超时抛 socket.timeout。"""
        b1, b2 = self._read(2)
        opcode = b1 & 0x0F
        masked = b2 & 0x80
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._read(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._read(8))[0]
        mask = self._read(4) if masked else None
        data = self._read(ln) if ln else b""
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))

        if opcode == 0x8:
            raise WsError("服务器关闭了连接")
        if opcode == 0x9:          # ping -> pong
            self._pong(data)
            return self.recv()
        if opcode == 0xA:
            return self.recv()
        if opcode == 0x1:
            return opcode, data.decode("utf-8", "replace")
        return opcode, ""

    def _pong(self, data: bytes):
        mask = os.urandom(4)
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes([0x8A, 0x80 | len(data)]) + mask + payload)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass
