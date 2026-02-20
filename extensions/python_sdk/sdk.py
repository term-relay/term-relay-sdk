#!/usr/bin/env python3
from __future__ import annotations

import base64
import errno
import fcntl
import json
import os
import pty
import signal
import struct
import subprocess
import sys
import termios
import threading
from typing import Any, Callable, Dict, Optional, Protocol, Tuple

JSONRPC_VERSION = "2.0"
PROTOCOL_VERSION = "v1"


def encode_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode_b64(text: str) -> bytes:
    return base64.b64decode(text)


class RPCError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def build_hello(
    *,
    ext_id: str,
    name: str,
    version: str,
    capabilities: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "id": ext_id,
        "name": name,
        "version": version,
        "protocol_version": PROTOCOL_VERSION,
        "capabilities": capabilities,
    }


class _JSONLineIO:
    def __init__(self):
        self._write_lock = threading.Lock()

    def send(self, obj: Dict[str, Any]) -> None:
        line = json.dumps(obj, separators=(",", ":"))
        with self._write_lock:
            sys.stdout.write(line)
            sys.stdout.write("\n")
            sys.stdout.flush()

    def lines(self):
        for raw in sys.stdin:
            yield raw


class SimpleIOAdapter(Protocol):
    def set_emitters(
        self,
        emit_output: Callable[[bytes], None],
        emit_exit: Callable[[str], None],
    ) -> None: ...

    def on_start(
        self, command: list[str], rows: int, cols: int, term: str
    ) -> Optional[Tuple[int, int]]: ...

    def on_input(self, data: bytes) -> None: ...

    def on_resize(self, rows: int, cols: int) -> None: ...

    def on_stop(self) -> None: ...


class SimpleIOServer:
    def __init__(self, adapter: SimpleIOAdapter):
        self._adapter = adapter
        self._io = _JSONLineIO()
        self._adapter.set_emitters(self.emit_output, self.emit_exit)

    def emit_output(self, data: bytes) -> None:
        if not data:
            return
        self._io.send({"type": "output", "data_b64": encode_b64(data)})

    def emit_exit(self, reason: str) -> None:
        self._io.send({"type": "exit", "reason": reason})

    def run(self) -> int:
        started = False
        for raw in self._io.lines():
            line = raw.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as err:
                self.emit_exit(f"invalid json: {err}")
                return 1
            if not isinstance(frame, dict):
                continue

            msg_type = str(frame.get("type", ""))
            if msg_type == "start":
                if started:
                    continue
                command = frame.get("command") or []
                rows = int(frame.get("rows") or 24)
                cols = int(frame.get("cols") or 80)
                term = str(frame.get("term") or "xterm-256color")
                try:
                    ready = self._adapter.on_start(command, rows, cols, term)
                except Exception as err:  # noqa: BLE001
                    self.emit_exit(str(err))
                    return 1
                started = True
                if ready is not None:
                    ready_rows, ready_cols = ready
                    self._io.send(
                        {"type": "ready", "rows": ready_rows, "cols": ready_cols}
                    )
                continue

            if msg_type == "input":
                data_b64 = frame.get("data_b64") or ""
                if not data_b64:
                    continue
                try:
                    data = decode_b64(data_b64)
                except Exception:  # noqa: BLE001
                    continue
                self._adapter.on_input(data)
                continue

            if msg_type == "resize":
                rows = int(frame.get("rows") or 0)
                cols = int(frame.get("cols") or 0)
                self._adapter.on_resize(rows, cols)
                continue

            if msg_type == "stop":
                self._adapter.on_stop()
                return 0

        if started:
            self._adapter.on_stop()
        return 0


class JsonRPCServer:
    def __init__(self):
        self._io = _JSONLineIO()
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
        self._on_close: Optional[Callable[[], None]] = None

    def on_close(self, fn: Callable[[], None]) -> None:
        self._on_close = fn

    def register(self, method: str, fn: Callable[[Dict[str, Any]], Any]) -> None:
        self._handlers[method] = fn

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self._io.send({"jsonrpc": JSONRPC_VERSION, "method": method, "params": params})

    def _result(self, req_id: Any, result: Any) -> Dict[str, Any]:
        return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "result": result}

    def _error(self, req_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {
            "jsonrpc": JSONRPC_VERSION,
            "id": req_id,
            "error": {"code": code, "message": message},
        }

    def run(self) -> int:
        for raw in self._io.lines():
            line = raw.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(req, dict):
                continue
            method = req.get("method")
            if not isinstance(method, str) or not method:
                continue

            req_id = req.get("id", 0)
            params = req.get("params") or {}
            if not isinstance(params, dict):
                self._io.send(
                    self._error(req_id, -32602, "params must be a json object")
                )
                continue

            fn = self._handlers.get(method)
            if fn is None:
                self._io.send(self._error(req_id, -32601, f"method not found: {method}"))
                continue

            try:
                result = fn(params)
                self._io.send(self._result(req_id, result))
            except RPCError as err:
                self._io.send(self._error(req_id, err.code, err.message))
            except Exception as err:  # noqa: BLE001
                self._io.send(self._error(req_id, -32603, str(err)))

        if self._on_close is not None:
            self._on_close()
        return 0


class SingleSessionRuntime(Protocol):
    handle: str
    rows: int
    cols: int

    def write_input(self, data: bytes) -> None: ...

    def resize(self, rows: int, cols: int) -> None: ...

    def stop(self) -> None: ...


class SingleSessionRPCServer:
    def __init__(
        self,
        *,
        hello_payload: Dict[str, Any],
        start_session: Callable[
            [
                Dict[str, Any],
                Callable[[str, bytes], None],
                Callable[[str, str], None],
            ],
            SingleSessionRuntime,
        ],
    ):
        self.rpc = JsonRPCServer()
        self._hello_payload = hello_payload
        self._start_session = start_session
        self._lock = threading.Lock()
        self._session: Optional[SingleSessionRuntime] = None

        self.rpc.register("ext.hello", self._hello)
        self.rpc.register("ext.health", self._health)
        self.rpc.register("ext.start", self._start)
        self.rpc.register("ext.input", self._input)
        self.rpc.register("ext.resize", self._resize)
        self.rpc.register("ext.stop", self._stop)
        self.rpc.on_close(self._cleanup)

    def _hello(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        return self._hello_payload

    def _health(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            return {"ok": True, "active": self._session is not None}

    def _start(self, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if self._session is not None:
                raise RPCError(4001, "session already running")

        runtime = self._start_session(params, self._emit_output, self._emit_exit)

        with self._lock:
            if self._session is not None:
                runtime.stop()
                raise RPCError(4001, "session already running")
            self._session = runtime

        return {
            "session_handle": runtime.handle,
            "rows": runtime.rows,
            "cols": runtime.cols,
        }

    def _input(self, params: Dict[str, Any]) -> Dict[str, Any]:
        runtime = self._require_session(params.get("session_handle") or "")
        data_b64 = params.get("data_b64") or ""
        try:
            data = decode_b64(data_b64)
        except Exception as err:  # noqa: BLE001
            raise RPCError(-32602, "invalid data_b64") from err
        runtime.write_input(data)
        return {"ok": True}

    def _resize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        runtime = self._require_session(params.get("session_handle") or "")
        rows = int(params.get("rows") or 0)
        cols = int(params.get("cols") or 0)
        if rows <= 0 or cols <= 0:
            raise RPCError(-32602, "rows and cols must be > 0")
        runtime.resize(rows, cols)
        return {"ok": True}

    def _stop(self, params: Dict[str, Any]) -> Dict[str, Any]:
        handle = params.get("session_handle") or ""
        with self._lock:
            runtime = self._session
            if runtime is not None and (not handle or handle == runtime.handle):
                runtime.stop()
                self._session = None
        return {"ok": True}

    def _require_session(self, handle: str) -> SingleSessionRuntime:
        with self._lock:
            runtime = self._session
            if runtime is None:
                raise RPCError(4004, "session not found")
            if handle and handle != runtime.handle:
                raise RPCError(4004, "session not found")
            return runtime

    def _emit_output(self, handle: str, data: bytes) -> None:
        if not data:
            return
        self.rpc.notify(
            "event.output",
            {"session_handle": handle, "data_b64": encode_b64(data)},
        )

    def _emit_exit(self, handle: str, reason: str) -> None:
        with self._lock:
            if self._session is not None and self._session.handle == handle:
                self._session = None
        self.rpc.notify(
            "event.exit",
            {"session_handle": handle, "reason": reason},
        )

    def _cleanup(self) -> None:
        with self._lock:
            runtime = self._session
            self._session = None
        if runtime is not None:
            runtime.stop()

    def run(self) -> int:
        return self.rpc.run()


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    if rows <= 0 or cols <= 0:
        return
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


class PTYSimpleIOAdapter(SimpleIOAdapter):
    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._master_fd: Optional[int] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._exit_once = threading.Event()
        self._emit_output: Callable[[bytes], None] = lambda _: None
        self._emit_exit: Callable[[str], None] = lambda _: None

    def set_emitters(
        self,
        emit_output: Callable[[bytes], None],
        emit_exit: Callable[[str], None],
    ) -> None:
        self._emit_output = emit_output
        self._emit_exit = emit_exit

    def on_start(
        self, command: list[str], rows: int, cols: int, term: str
    ) -> Optional[Tuple[int, int]]:
        if not command:
            raise ValueError("start.command is required")
        if rows <= 0:
            rows = 24
        if cols <= 0:
            cols = 80
        if not term:
            term = "xterm-256color"

        master_fd, slave_fd = pty.openpty()
        env = os.environ.copy()
        env["TERM"] = term
        proc = subprocess.Popen(
            command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            preexec_fn=os.setsid,
            close_fds=True,
        )
        os.close(slave_fd)
        _set_winsize(master_fd, rows, cols)

        self._proc = proc
        self._master_fd = master_fd

        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._wait_loop, daemon=True).start()
        return rows, cols

    def on_input(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if self._master_fd is None:
                return
            try:
                os.write(self._master_fd, data)
            except OSError:
                return

    def on_resize(self, rows: int, cols: int) -> None:
        if rows <= 0 or cols <= 0:
            return
        with self._lock:
            if self._master_fd is None:
                return
            try:
                _set_winsize(self._master_fd, rows, cols)
            except OSError:
                return

    def on_stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            if self._master_fd is not None:
                try:
                    os.close(self._master_fd)
                except OSError:
                    pass
                self._master_fd = None
            proc = self._proc
            self._proc = None

        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
            except OSError:
                pass

    def _read_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                fd = self._master_fd
            if fd is None:
                return
            try:
                chunk = os.read(fd, 4096)
            except OSError as err:
                if err.errno in (errno.EIO, errno.EBADF):
                    return
                return
            if not chunk:
                return
            self._emit_output(chunk)

    def _wait_loop(self) -> None:
        proc = self._proc
        if proc is None:
            self._notify_exit("EOF")
            return
        code = proc.wait()
        if code == 0:
            self._notify_exit("EOF")
            return
        self._notify_exit(f"exit status {code}")

    def _notify_exit(self, reason: str) -> None:
        if self._exit_once.is_set():
            return
        self._exit_once.set()
        self._emit_exit(reason)
