from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python_sdk.sdk import decode_b64, encode_b64

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "python-iterm2" / "main.py"


class VirtualPaneServer:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._server: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._reader = None
        self._writer = None
        self._ready = threading.Event()
        self._frames: queue.Queue[dict[str, Any]] = queue.Queue()
        self._write_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.socket_path)
        self._server.listen(1)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        assert self._server is not None
        conn, _ = self._server.accept()
        self._conn = conn
        self._reader = conn.makefile("rb")
        self._writer = conn.makefile("wb")
        self._ready.set()
        while True:
            if self._reader is None:
                return
            line = self._reader.readline()
            if not line:
                return
            try:
                frame = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(frame, dict):
                self._frames.put(frame)

    def expect_frame(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AssertionError("timed out waiting virtual pane frame")
            frame = self._frames.get(timeout=remaining)
            if predicate(frame):
                return frame

    def send_frame(self, frame: dict[str, Any], timeout: float = 2.0) -> None:
        if not self._ready.wait(timeout=timeout):
            raise AssertionError("virtual pane was not connected")
        payload = (json.dumps(frame, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            if self._writer is None:
                raise AssertionError("virtual pane writer is not ready")
            self._writer.write(payload)
            self._writer.flush()

    def close(self) -> None:
        if self._reader is not None:
            try:
                self._reader.close()
            except OSError:
                pass
            self._reader = None
        if self._writer is not None:
            try:
                self._writer.close()
            except OSError:
                pass
            self._writer = None
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None


class RelayProcess:
    def __init__(self, socket_path: str):
        env = os.environ.copy()
        env["TERM_RELAY_ITERM2_BRIDGE_SOCKET"] = socket_path
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(SCRIPT_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._buffer: list[dict[str, Any]] = []
        self._stderr_lines: list[str] = []
        self._stdout_thread = threading.Thread(target=self._stdout_loop, daemon=True)
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _stdout_loop(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                self._messages.put(msg)

    def _stderr_loop(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            line = raw.rstrip("\n")
            if line:
                self._stderr_lines.append(line)

    def request(
        self,
        req_id: int,
        method: str,
        params: dict[str, Any],
    ) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def wait_message(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        deadline = time.time() + timeout
        while True:
            for idx, buffered in enumerate(self._buffer):
                if predicate(buffered):
                    return self._buffer.pop(idx)
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AssertionError(self._timeout_message())
            try:
                msg = self._messages.get(timeout=remaining)
            except queue.Empty as err:
                raise AssertionError(self._timeout_message()) from err
            if predicate(msg):
                return msg
            self._buffer.append(msg)

    def wait_response(self, req_id: int, timeout: float = 2.0) -> dict[str, Any]:
        return self.wait_message(
            lambda msg: msg.get("id") == req_id
            and ("result" in msg or "error" in msg),
            timeout=timeout,
        )

    def wait_notification(
        self, method: str, timeout: float = 2.0
    ) -> dict[str, Any]:
        return self.wait_message(
            lambda msg: msg.get("method") == method and "params" in msg,
            timeout=timeout,
        )

    def close(self) -> None:
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=1.0)
        if self.proc.stdout is not None:
            try:
                self.proc.stdout.close()
            except OSError:
                pass
        if self.proc.stderr is not None:
            try:
                self.proc.stderr.close()
            except OSError:
                pass
        self._stdout_thread.join(timeout=1.0)
        self._stderr_thread.join(timeout=1.0)

    def _timeout_message(self) -> str:
        rc = self.proc.poll()
        stderr = "; ".join(self._stderr_lines[-6:])
        return (
            "timed out waiting relay message "
            f"(returncode={rc}, stderr_tail={stderr})"
        )


class Iterm2RelayProcessTests(unittest.TestCase):
    def test_virtual_pane_roundtrip_without_hub(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = os.path.join(tmp, "iterm2-bridge.sock")
            pane = VirtualPaneServer(sock_path)
            pane.start()
            relay = RelayProcess(sock_path)
            try:
                relay.request(
                    1,
                    "ext.start",
                    {
                        "target": "iterm2://pane/fake-1",
                        "rows": 24,
                        "cols": 80,
                        "term": "xterm-256color",
                    },
                )
                attach = pane.expect_frame(lambda frame: frame.get("type") == "attach")
                self.assertEqual("iterm2://pane/fake-1", attach.get("target"))

                pane.send_frame({"type": "attached", "rows": 35, "cols": 90})
                start_resp = relay.wait_response(1)
                self.assertIn("result", start_resp)
                start_result = start_resp["result"]
                session_handle = start_result["session_handle"]
                self.assertEqual(35, start_result["rows"])
                self.assertEqual(90, start_result["cols"])

                pane.send_frame(
                    {"type": "output", "data_b64": encode_b64(b"virtual-prompt$ ")}
                )
                output_evt = relay.wait_notification("event.output")
                self.assertEqual(session_handle, output_evt["params"]["session_handle"])
                self.assertEqual(
                    b"virtual-prompt$ ",
                    decode_b64(output_evt["params"]["data_b64"]),
                )

                relay.request(
                    2,
                    "ext.input",
                    {
                        "session_handle": session_handle,
                        "data_b64": encode_b64(b"ls\r"),
                    },
                )
                input_resp = relay.wait_response(2)
                self.assertIn("result", input_resp)
                input_frame = pane.expect_frame(lambda frame: frame.get("type") == "input")
                self.assertEqual(b"ls\r", decode_b64(input_frame["data_b64"]))

                relay.request(
                    3,
                    "ext.resize",
                    {"session_handle": session_handle, "rows": 40, "cols": 120},
                )
                resize_resp = relay.wait_response(3)
                self.assertIn("result", resize_resp)
                resize_frame = pane.expect_frame(
                    lambda frame: frame.get("type") == "resize"
                )
                self.assertEqual(40, int(resize_frame["rows"]))
                self.assertEqual(120, int(resize_frame["cols"]))

                pane.send_frame({"type": "exit", "reason": "virtual pane closed"})
                exit_evt = relay.wait_notification("event.exit")
                self.assertEqual("virtual pane closed", exit_evt["params"]["reason"])
                self.assertEqual(session_handle, exit_evt["params"]["session_handle"])
            finally:
                relay.close()
                pane.close()

    def test_start_returns_error_when_virtual_pane_reports_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = os.path.join(tmp, "iterm2-bridge.sock")
            pane = VirtualPaneServer(sock_path)
            pane.start()
            relay = RelayProcess(sock_path)
            try:
                relay.request(1, "ext.start", {"target": "iterm2://pane/missing"})
                pane.expect_frame(lambda frame: frame.get("type") == "attach")
                pane.send_frame({"type": "error", "message": "pane not found"})
                start_resp = relay.wait_response(1)
                self.assertIn("error", start_resp)
                self.assertEqual(-32603, start_resp["error"]["code"])
                self.assertIn("pane not found", start_resp["error"]["message"])
            finally:
                relay.close()
                pane.close()


if __name__ == "__main__":
    unittest.main()
