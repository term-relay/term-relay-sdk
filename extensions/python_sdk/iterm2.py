from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any, Callable, Dict, Optional

from .bridge import TerminalBridgeStart, start_terminal_bridge_session
from .sdk import RPCError, decode_b64, encode_b64

ERR_INVALID_PARAMS = -32602


class Iterm2SocketTransport:
    def __init__(
        self,
        *,
        socket_path: str,
        connect_timeout: float = 2.0,
        sock_factory: Optional[Callable[[], socket.socket]] = None,
    ):
        self._socket_path = socket_path
        self._connect_timeout = connect_timeout
        self._sock_factory = sock_factory

        self._sock: Optional[socket.socket] = None
        self._reader = None
        self._writer = None
        self._read_thread: Optional[threading.Thread] = None

        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._closed = False
        self._exit_emitted = False

        self._on_output: Callable[[bytes], None] = lambda _data: None
        self._on_exit: Callable[[str], None] = lambda _reason: None

        self._attached_event = threading.Event()
        self._attached_rows = 0
        self._attached_cols = 0
        self._attach_error: Optional[str] = None

    def set_event_handlers(
        self,
        on_output: Callable[[bytes], None],
        on_exit: Callable[[str], None],
    ) -> None:
        self._on_output = on_output
        self._on_exit = on_exit

    def connect(self, start: TerminalBridgeStart) -> Optional[tuple[int, int]]:
        if not start.target:
            raise RuntimeError("iterm2 target is required")
        self._open_socket()
        self._send_frame(
            {
                "type": "attach",
                "target": start.target,
                "command": start.command,
                "rows": start.rows,
                "cols": start.cols,
                "term": start.term,
            }
        )
        if not self._attached_event.wait(timeout=self._connect_timeout):
            self.close()
            raise RuntimeError("iterm2 bridge attach timeout")
        if self._attach_error:
            self.close()
            raise RuntimeError(self._attach_error)
        if self._attached_rows > 0 and self._attached_cols > 0:
            return self._attached_rows, self._attached_cols
        return None

    def send_input(self, data: bytes) -> None:
        if not data:
            return
        self._send_frame({"type": "input", "data_b64": encode_b64(data)})

    def send_resize(self, rows: int, cols: int) -> None:
        if rows <= 0 or cols <= 0:
            return
        self._send_frame({"type": "resize", "rows": rows, "cols": cols})

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._send_frame({"type": "detach"})
        except Exception:  # noqa: BLE001
            pass
        self._safe_close_io()

    def _open_socket(self) -> None:
        if self._sock_factory is not None:
            sock = self._sock_factory()
        else:
            if not self._socket_path:
                raise RuntimeError("iterm2 bridge socket path is empty")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self._connect_timeout)
            sock.connect(self._socket_path)
            sock.settimeout(None)
        self._sock = sock
        self._reader = sock.makefile("rb")
        self._writer = sock.makefile("wb")
        self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._read_thread.start()

    def _send_frame(self, frame: Dict[str, Any]) -> None:
        payload = (json.dumps(frame, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            if self._writer is None:
                raise RuntimeError("iterm2 bridge is not connected")
            self._writer.write(payload)
            self._writer.flush()

    def _read_loop(self) -> None:
        while True:
            if self._reader is None:
                break
            line = self._reader.readline()
            if not line:
                if not self._attached_event.is_set():
                    self._attach_error = "iterm2 bridge disconnected before attach"
                    self._attached_event.set()
                self._emit_exit_once("iterm2 bridge disconnected")
                break
            try:
                frame = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(frame, dict):
                continue
            self._handle_frame(frame)

    def _handle_frame(self, frame: Dict[str, Any]) -> None:
        msg_type = str(frame.get("type") or "")
        if msg_type == "attached":
            self._attached_rows = int(frame.get("rows") or 0)
            self._attached_cols = int(frame.get("cols") or 0)
            self._attached_event.set()
            return
        if msg_type == "error":
            message = str(frame.get("message") or "iterm2 bridge error")
            if not self._attached_event.is_set():
                self._attach_error = message
                self._attached_event.set()
            self._emit_exit_once(message)
            return
        if msg_type == "output":
            data_b64 = frame.get("data_b64") or ""
            if not data_b64:
                return
            try:
                data = decode_b64(data_b64)
            except Exception:  # noqa: BLE001
                return
            self._on_output(data)
            return
        if msg_type == "exit":
            reason = str(frame.get("reason") or "iterm2 bridge exit")
            if not self._attached_event.is_set():
                self._attach_error = reason
                self._attached_event.set()
            self._emit_exit_once(reason)

    def _emit_exit_once(self, reason: str) -> None:
        with self._state_lock:
            if self._exit_emitted:
                return
            self._exit_emitted = True
        self._on_exit(reason)

    def _safe_close_io(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        writer = self._writer
        self._writer = None
        if writer is not None:
            try:
                writer.close()
            except OSError:
                pass

        reader = self._reader
        self._reader = None
        if reader is not None:
            try:
                reader.close()
            except OSError:
                pass

        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def start_iterm2_socket_session(
    params: Dict[str, Any],
    emit_output: Callable[[str, bytes], None],
    emit_exit: Callable[[str, str], None],
    *,
    default_socket_path: str,
    connect_timeout: float = 2.0,
) -> Any:
    socket_path = str(params.get("bridge_socket") or default_socket_path).strip()
    if not socket_path:
        raise RPCError(ERR_INVALID_PARAMS, "bridge_socket is required")

    return start_terminal_bridge_session(
        params,
        emit_output,
        emit_exit,
        transport_factory=lambda _start: Iterm2SocketTransport(
            socket_path=socket_path,
            connect_timeout=connect_timeout,
        ),
    )


def list_iterm2_targets(
    *,
    socket_path: str,
    connect_timeout: float = 2.0,
) -> list[dict[str, Any]]:
    if not socket_path:
        raise RPCError(ERR_INVALID_PARAMS, "bridge_socket is required")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    reader = None
    writer = None
    try:
        sock.settimeout(connect_timeout)
        sock.connect(socket_path)
        sock.settimeout(None)
        reader = sock.makefile("rb")
        writer = sock.makefile("wb")

        writer.write(b'{"type":"list_targets"}\n')
        writer.flush()

        deadline = time.time() + connect_timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise RuntimeError("iterm2 bridge list_targets timeout")
            sock.settimeout(remaining)
            line = reader.readline()
            sock.settimeout(None)
            if not line:
                raise RuntimeError("iterm2 bridge disconnected during list_targets")

            try:
                frame = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(frame, dict):
                continue

            msg_type = str(frame.get("type") or "")
            if msg_type == "targets":
                raw = frame.get("targets")
                if not isinstance(raw, list):
                    return []
                targets: list[dict[str, Any]] = []
                for item in raw:
                    if isinstance(item, dict):
                        targets.append(item)
                return targets

            if msg_type == "error":
                message = str(frame.get("message") or "iterm2 bridge error")
                raise RuntimeError(message)
    finally:
        if reader is not None:
            try:
                reader.close()
            except OSError:
                pass
        if writer is not None:
            try:
                writer.close()
            except OSError:
                pass
        try:
            sock.close()
        except OSError:
            pass
