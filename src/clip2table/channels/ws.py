"""Minimal RFC 6455 WebSocket client for DevTools, zero dependencies.

CDP speaks WebSocket and it runs on localhost, so the whole TLS/HTTP client
stack is dead weight: `ws://` only, text frames only, no permessage-deflate.
The parts that are load-bearing are fragmentation (a long page's DOM does not
fit one frame) and pings (Chrome sends them unprompted and drops the socket
without a pong).

Why roll this instead of using the `websockets` package: the tool's promise is
that `pip install clip2table` pulls nothing. A dependency that only exists to
talk to localhost would break that promise for every user.
"""
from __future__ import annotations

import base64
import os
import socket
import struct

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = (
    0x0, 0x1, 0x2, 0x8, 0x9, 0xA)


class WSError(RuntimeError):
    """Transport-level failure. The caller turns this into a ChannelError."""


class WS:
    def __init__(self, url: str, timeout: float = 20.0):
        if not url.startswith("ws://"):
            raise WSError(
                f"只支持 ws://（DevTools 在本机），实际是 {url[:40]!r}")
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        self.host, _, port_s = hostport.partition(":")
        self.port = int(port_s or 80)
        self.timeout = timeout
        try:
            self.sock = socket.create_connection(
                (self.host, self.port), timeout=timeout)
        except OSError as e:
            raise WSError(f"连不上 {self.host}:{self.port}：{e}") from None
        self.sock.settimeout(timeout)
        self._buf = b""
        self._handshake("/" + path)

    # ------------------------------------------------------------- handshake
    def _handshake(self, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        try:
            self.sock.sendall(req.encode())
            while b"\r\n\r\n" not in self._buf:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise WSError("握手时连接被关闭")
                self._buf += chunk
        except OSError as e:
            raise WSError(f"握手失败：{e}") from None
        head, _, self._buf = self._buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise WSError(f"握手被拒：{status[:120]!r}")

    # --------------------------------------------------------------- framing
    def _send_frame(self, opcode: int, payload: bytes) -> None:
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
        try:
            self.sock.sendall(bytes(header) + masked)
        except OSError as e:
            raise WSError(f"发送失败：{e}") from None

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                raise WSError("等 DevTools 回包超时") from None
            except OSError as e:
                raise WSError(f"接收失败：{e}") from None
            if not chunk:
                raise WSError("DevTools 关闭了连接")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self) -> str:
        """One complete text message. Pings are answered, fragments are joined."""
        parts: list[bytes] = []
        while True:
            b0, b1 = self._recv_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            if b1 & 0x80:
                raise WSError("服务端不应加掩码")
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._recv_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._recv_exact(8))[0]
            data = self._recv_exact(ln) if ln else b""

            if opcode == OP_PING:
                self._send_frame(OP_PONG, data)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                raise WSError("DevTools 主动关闭")
            if opcode in (OP_TEXT, OP_BIN, OP_CONT):
                parts.append(data)
                if fin:
                    return b"".join(parts).decode("utf-8", errors="replace")
                continue
            raise WSError(f"没见过的 opcode {opcode}")

    def send(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode())

    def close(self) -> None:
        try:
            self._send_frame(OP_CLOSE, b"")
        except (OSError, WSError):
            pass
        try:
            self.sock.close()
        except OSError:
            pass
