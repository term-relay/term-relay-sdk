from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Protocol

from .sdk import RPCError

ERR_INVALID_PARAMS = -32602


@dataclass(frozen=True)
class TerminalBridgeStart:
    target: str
    command: list[str]
    rows: int
    cols: int
    term: str


class TerminalBridgeTransport(Protocol):
    def set_event_handlers(
        self,
        on_output: Callable[[bytes], None],
        on_exit: Callable[[str], None],
    ) -> None: ...

    def connect(self, start: TerminalBridgeStart) -> Optional[tuple[int, int]]: ...

    def send_input(self, data: bytes) -> None: ...

    def send_resize(self, rows: int, cols: int) -> None: ...

    def close(self) -> None: ...


class TerminalBridgeRuntime:
    def __init__(
        self,
        *,
        transport: TerminalBridgeTransport,
        start: TerminalBridgeStart,
        emit_output: Callable[[str, bytes], None],
        emit_exit: Callable[[str, str], None],
    ):
        self.handle = secrets.token_hex(16)
        self.rows = start.rows
        self.cols = start.cols
        self.target = start.target
        self.term = start.term

        self._transport = transport
        self._emit_output = emit_output
        self._emit_exit = emit_exit

        self._lock = threading.Lock()
        self._stopped = False
        self._exit_sent = False

        self._transport.set_event_handlers(self._on_output, self._on_exit)
        ready = self._transport.connect(start)
        if ready is not None:
            ready_rows, ready_cols = ready
            if ready_rows > 0:
                self.rows = ready_rows
            if ready_cols > 0:
                self.cols = ready_cols

    def write_input(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if self._stopped:
                return
        self._transport.send_input(data)

    def resize(self, rows: int, cols: int) -> None:
        if rows <= 0 or cols <= 0:
            return
        with self._lock:
            if self._stopped:
                return
            self.rows = rows
            self.cols = cols
        self._transport.send_resize(rows, cols)

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        self._transport.close()

    def _on_output(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if self._stopped:
                return
        self._emit_output(self.handle, data)

    def _on_exit(self, reason: str) -> None:
        with self._lock:
            if self._exit_sent:
                return
            self._exit_sent = True
        self._emit_exit(self.handle, reason)


def parse_terminal_bridge_start(
    params: Dict[str, Any],
    *,
    default_rows: int = 24,
    default_cols: int = 80,
    default_term: str = "xterm-256color",
) -> TerminalBridgeStart:
    command = list(params.get("command") or [])
    target = str(params.get("target") or "").strip()
    if not target and command:
        target = str(command[0]).strip()
    if not target:
        raise RPCError(ERR_INVALID_PARAMS, "target is required")

    rows = int(params.get("rows") or default_rows)
    cols = int(params.get("cols") or default_cols)
    term = str(params.get("term") or default_term)
    if rows <= 0:
        rows = default_rows
    if cols <= 0:
        cols = default_cols
    if not term:
        term = default_term

    return TerminalBridgeStart(
        target=target,
        command=command,
        rows=rows,
        cols=cols,
        term=term,
    )


def start_terminal_bridge_session(
    params: Dict[str, Any],
    emit_output: Callable[[str, bytes], None],
    emit_exit: Callable[[str, str], None],
    *,
    transport_factory: Callable[[TerminalBridgeStart], TerminalBridgeTransport],
) -> TerminalBridgeRuntime:
    start = parse_terminal_bridge_start(params)
    transport = transport_factory(start)
    return TerminalBridgeRuntime(
        transport=transport,
        start=start,
        emit_output=emit_output,
        emit_exit=emit_exit,
    )
