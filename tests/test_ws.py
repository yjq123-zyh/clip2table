"""WebSocket framing tests, without a browser.

A socketpair stands in for the DevTools connection: the client side is a WS
object with its socket swapped in, the server side is the raw other end. That
is enough to check the two things that break CDP in practice - client masking
and fragmentation - without launching anything.
"""
from __future__ import annotations

import socket
import struct
import unittest

from clip2table.channels.ws import OP_CLOSE, OP_PING, OP_TEXT, WS, WSError


def make_pair() -> tuple[WS, socket.socket]:
    """A WS whose socket is one end of a socketpair; the peer is returned."""
    client_sock, server_sock = socket.socketpair()
    ws = object.__new__(WS)
    ws.sock = client_sock
    ws.host = "127.0.0.1"
    ws.port = 0
    ws.timeout = 5.0
    ws._buf = b""
    return ws, server_sock


class TestFraming(unittest.TestCase):
    def test_send_masks_and_sets_text_opcode(self):
        ws, peer = make_pair()
        try:
            ws.send("hi")
            frame = peer.recv(1024)
        finally:
            ws.sock.close()
            peer.close()
        self.assertEqual(frame[0] & 0x0F, OP_TEXT)
        self.assertTrue(frame[0] & 0x80, "FIN must be set on a whole message")
        self.assertTrue(frame[1] & 0x80, "client frames MUST be masked")
        length = frame[1] & 0x7F
        self.assertEqual(length, 2)
        mask = frame[2:6]
        payload = frame[6:8]
        self.assertEqual(bytes(b ^ mask[i % 4] for i, b in enumerate(payload)),
                         b"hi")

    def test_long_payload_uses_64bit_length(self):
        big = "x" * 70000
        ws, peer = make_pair()
        try:
            ws.send(big)
            head = peer.recv(10)
        finally:
            ws.sock.close()
            peer.close()
        self.assertEqual(head[1] & 0x7F, 127)
        self.assertEqual(struct.unpack(">Q", head[2:10])[0], 70000)

    def test_recv_joins_fragments(self):
        ws, peer = make_pair()
        try:
            # text frame, FIN=0, then a continuation with FIN=1
            peer.sendall(b"\x01\x03abc")
            peer.sendall(b"\x80\x02de")
            self.assertEqual(ws.recv(), "abcde")
        finally:
            ws.sock.close()
            peer.close()

    def test_ping_is_answered_and_skipped(self):
        ws, peer = make_pair()
        try:
            peer.sendall(b"\x89\x01z")            # ping "z"
            peer.sendall(b"\x81\x02ok")           # text "ok"
            self.assertEqual(ws.recv(), "ok")
            pong = peer.recv(64)
            self.assertEqual(pong[0] & 0x0F, 0xA, "must reply with a pong")
            self.assertTrue(pong[1] & 0x80)
        finally:
            ws.sock.close()
            peer.close()

    def test_server_masking_is_rejected(self):
        ws, peer = make_pair()
        try:
            peer.sendall(b"\x81\x82\x00\x00\x00\x00hi")
            with self.assertRaises(WSError):
                ws.recv()
        finally:
            ws.sock.close()
            peer.close()

    def test_close_frame_raises(self):
        ws, peer = make_pair()
        try:
            peer.sendall(b"\x88\x00")
            with self.assertRaises(WSError):
                ws.recv()
        finally:
            ws.sock.close()
            peer.close()

    def test_connection_drop_mid_frame_raises(self):
        ws, peer = make_pair()
        try:
            peer.sendall(b"\x81\x0aabc")          # promises 10 bytes, sends 3
            peer.close()
            with self.assertRaises(WSError):
                ws.recv()
        finally:
            ws.sock.close()

    def test_non_ws_url_refused_before_connecting(self):
        with self.assertRaises(WSError):
            WS("wss://example.com/x")


if __name__ == "__main__":
    unittest.main()
