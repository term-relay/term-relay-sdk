from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python_sdk.bridge import TerminalBridgeStart
from python_sdk.iterm2 import (
    Iterm2SocketTransport,
    list_iterm2_targets,
    start_iterm2_socket_session,
)
from python_sdk.sdk import RPCError, decode_b64, encode_b64


def _read_json_line(reader) -> dict:
    line = reader.readline()
    if not line:
        return {}
    return json.loads(line.decode("utf-8"))


def _write_json_line(writer, obj: dict) -> None:
    writer.write((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))
    writer.flush()


class Iterm2SocketTransportTests(unittest.TestCase):
    def test_connect_receives_attached_and_output(self):
        outputs: list[bytes] = []
        exits: list[str] = []
        exit_event = threading.Event()

        with tempfile.TemporaryDirectory() as tmp:
            sock_path = os.path.join(tmp, "bridge.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(sock_path)
            server.listen(1)

            attach_frame: dict = {}

            def server_thread():
                nonlocal attach_frame
                conn, _ = server.accept()
                with conn:
                    reader = conn.makefile("rb")
                    writer = conn.makefile("wb")
                    attach_frame = _read_json_line(reader)
                    _write_json_line(writer, {"type": "attached", "rows": 41, "cols": 121})
                    _write_json_line(
                        writer,
                        {"type": "output", "data_b64": encode_b64(b"iterm2-out")},
                    )
                    _write_json_line(writer, {"type": "exit", "reason": "done"})
                    time.sleep(0.05)

            th = threading.Thread(target=server_thread, daemon=True)
            th.start()

            transport = Iterm2SocketTransport(socket_path=sock_path, connect_timeout=1.0)
            transport.set_event_handlers(
                lambda data: outputs.append(data),
                lambda reason: (exits.append(reason), exit_event.set()),
            )
            ready = transport.connect(
                TerminalBridgeStart(
                    target="iterm2://pane/abc",
                    command=["iterm2://pane/abc"],
                    rows=24,
                    cols=80,
                    term="xterm-256color",
                )
            )
            self.assertEqual((41, 121), ready)
            self.assertTrue(exit_event.wait(timeout=1.0))
            transport.close()
            server.close()

            self.assertEqual("attach", attach_frame.get("type"))
            self.assertEqual("iterm2://pane/abc", attach_frame.get("target"))
            self.assertEqual([b"iterm2-out"], outputs)
            self.assertEqual(["done"], exits)

    def test_send_input_and_resize_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = os.path.join(tmp, "bridge.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(sock_path)
            server.listen(1)

            frames: list[dict] = []
            done = threading.Event()

            def server_thread():
                conn, _ = server.accept()
                with conn:
                    reader = conn.makefile("rb")
                    writer = conn.makefile("wb")
                    frames.append(_read_json_line(reader))  # attach
                    _write_json_line(writer, {"type": "attached", "rows": 24, "cols": 80})
                    frames.append(_read_json_line(reader))  # input
                    frames.append(_read_json_line(reader))  # resize
                    frames.append(_read_json_line(reader))  # detach (best effort)
                    done.set()

            th = threading.Thread(target=server_thread, daemon=True)
            th.start()

            transport = Iterm2SocketTransport(socket_path=sock_path, connect_timeout=1.0)
            transport.connect(
                TerminalBridgeStart(
                    target="iterm2://pane/xyz",
                    command=["iterm2://pane/xyz"],
                    rows=30,
                    cols=100,
                    term="xterm-256color",
                )
            )
            transport.send_input(b"abc")
            transport.send_resize(50, 120)
            transport.close()

            self.assertTrue(done.wait(timeout=1.0))
            server.close()
            self.assertGreaterEqual(len(frames), 3)
            self.assertEqual("attach", frames[0].get("type"))
            self.assertEqual("input", frames[1].get("type"))
            self.assertEqual(b"abc", decode_b64(frames[1].get("data_b64") or ""))
            self.assertEqual("resize", frames[2].get("type"))
            self.assertEqual(50, int(frames[2].get("rows") or 0))
            self.assertEqual(120, int(frames[2].get("cols") or 0))

    def test_start_iterm2_socket_session_requires_bridge_socket(self):
        with self.assertRaises(RPCError):
            start_iterm2_socket_session(
                {"target": "iterm2://pane/1"},
                emit_output=lambda _handle, _data: None,
                emit_exit=lambda _handle, _reason: None,
                default_socket_path="",
            )

    def test_list_iterm2_targets_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = os.path.join(tmp, "bridge.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(sock_path)
            server.listen(1)

            def server_thread():
                conn, _ = server.accept()
                with conn:
                    reader = conn.makefile("rb")
                    writer = conn.makefile("wb")
                    frame = _read_json_line(reader)
                    self.assertEqual("list_targets", frame.get("type"))
                    _write_json_line(
                        writer,
                        {
                            "type": "targets",
                            "targets": [
                                {"id": "pane-1", "title": "zsh", "rows": 35, "cols": 71},
                                {"id": "pane-2", "title": "bash", "rows": 24, "cols": 80},
                            ],
                        },
                    )

            th = threading.Thread(target=server_thread, daemon=True)
            th.start()

            targets = list_iterm2_targets(socket_path=sock_path, connect_timeout=1.0)
            server.close()

            self.assertEqual(2, len(targets))
            self.assertEqual("pane-1", targets[0].get("id"))
            self.assertEqual("pane-2", targets[1].get("id"))

    def test_list_iterm2_targets_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = os.path.join(tmp, "bridge.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(sock_path)
            server.listen(1)

            def server_thread():
                conn, _ = server.accept()
                with conn:
                    reader = conn.makefile("rb")
                    writer = conn.makefile("wb")
                    frame = _read_json_line(reader)
                    self.assertEqual("list_targets", frame.get("type"))
                    _write_json_line(writer, {"type": "error", "message": "not supported"})

            th = threading.Thread(target=server_thread, daemon=True)
            th.start()

            with self.assertRaisesRegex(RuntimeError, "not supported"):
                list_iterm2_targets(socket_path=sock_path, connect_timeout=1.0)
            server.close()


if __name__ == "__main__":
    unittest.main()
