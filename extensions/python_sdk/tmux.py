from __future__ import annotations

import re
import secrets
import signal
import subprocess
import threading
from typing import Any, Dict, Tuple

from .sdk import RPCError

ERR_INVALID_PARAMS = -32602

RELAY_ORIGIN_PANE_OPT = "@term_relay_origin"
OUTPUT_RE = re.compile(br"^%output (%\d+) (.*)$")


def decode_octal(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        if (
            data[i] == 0x5C
            and i + 3 < n
            and 0x30 <= data[i + 1] <= 0x37
            and 0x30 <= data[i + 2] <= 0x37
            and 0x30 <= data[i + 3] <= 0x37
        ):
            out.append(int(data[i + 1 : i + 4], 8))
            i += 4
            continue
        out.append(data[i])
        i += 1
    return bytes(out)


def tmux_cmd(args, check: bool = True) -> bytes:
    proc = subprocess.run(
        ["tmux", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        if not err:
            err = f"tmux command failed: {' '.join(args)}"
        raise RuntimeError(err)
    return proc.stdout


def pane_option_value(target: str, option: str) -> str:
    if not option.startswith("@"):
        raise RuntimeError(f"invalid pane option name: {option}")
    raw = tmux_cmd(["display-message", "-t", target, "-p", f"#{{{option}}}"])
    value = raw.decode("utf-8", "replace").strip()
    if value == option:
        return ""
    return value


def parse_tmux_start_command(command) -> Tuple[str, bool]:
    if not command:
        raise RPCError(ERR_INVALID_PARAMS, "tmux target is required (example: %0)")

    args = list(command)
    if args and args[0] == "share":
        args = args[1:]

    allow_nested = False
    target = ""
    for arg in args:
        if arg in ("--allow-nested", "-allow-nested"):
            allow_nested = True
            continue
        if arg.startswith("-"):
            raise RPCError(ERR_INVALID_PARAMS, f"unknown option: {arg}")
        if target:
            raise RPCError(ERR_INVALID_PARAMS, f"too many positional arguments: {arg}")
        target = arg

    if not target:
        raise RPCError(ERR_INVALID_PARAMS, "tmux target is required (example: %0)")
    return target, allow_nested


class TmuxControlSession:
    def __init__(self, target: str, emit_output, emit_exit):
        self.handle = secrets.token_hex(16)
        self.target = target
        self.target_pane = ""
        self.session_name = ""
        self.rows = 0
        self.cols = 0

        self._emit_output = emit_output
        self._emit_exit = emit_exit
        self._proc = None
        self._write_lock = threading.Lock()
        self._stopped = threading.Event()
        self._exit_once = threading.Event()

    def start(self, rows: int, cols: int) -> Tuple[int, int]:
        if rows <= 0:
            rows = 24
        if cols <= 0:
            cols = 80
        self.target_pane = tmux_cmd(
            ["display-message", "-t", self.target, "-p", "#{pane_id}"]
        ).decode("utf-8", "replace").strip()
        self.session_name = tmux_cmd(
            ["display-message", "-t", self.target, "-p", "#{session_name}"]
        ).decode("utf-8", "replace").strip()

        self._proc = subprocess.Popen(
            ["tmux", "-C", "attach", "-t", self.session_name],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.rows = rows
        self.cols = cols
        self.resize(rows, cols)

        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._wait_loop, daemon=True).start()
        self.capture_and_emit()
        return rows, cols

    def write_input(self, data: bytes) -> None:
        if not data:
            return
        for b in data:
            self._send_cmd(f"send-keys -t {self.target} -H {b:02x}")

    def resize(self, rows: int, cols: int) -> None:
        if rows <= 0 or cols <= 0:
            return
        self.rows = rows
        self.cols = cols
        self._send_cmd(f"refresh-client -C {cols}x{rows}")

    def stop(self) -> None:
        self._stopped.set()
        if self._proc is None:
            return
        with self._write_lock:
            if self._proc.stdin:
                try:
                    self._proc.stdin.close()
                except OSError:
                    pass
        if self._proc.poll() is None:
            try:
                self._proc.send_signal(signal.SIGINT)
            except OSError:
                pass

    def capture_and_emit(self) -> None:
        try:
            data = tmux_cmd(
                ["capture-pane", "-t", self.target, "-e", "-p", "-S", "-", "-E", "-"]
            )
        except RuntimeError:
            return
        if data:
            self._emit_output(self.handle, data.replace(b"\n", b"\r\n"))

    def _send_cmd(self, cmd: str) -> None:
        if self._proc is None or self._proc.stdin is None or self._stopped.is_set():
            return
        payload = (cmd + "\n").encode("utf-8")
        with self._write_lock:
            try:
                self._proc.stdin.write(payload)
                self._proc.stdin.flush()
            except OSError:
                return

    def _read_loop(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        while not self._stopped.is_set():
            line = self._proc.stdout.readline()
            if not line:
                return
            match = OUTPUT_RE.match(line.rstrip(b"\r\n"))
            if not match:
                continue
            pane = match.group(1).decode("ascii", "ignore")
            if self.target_pane and pane and pane != self.target_pane:
                continue
            payload = decode_octal(match.group(2))
            if payload:
                self._emit_output(self.handle, payload)

    def _wait_loop(self) -> None:
        if self._proc is None:
            self._emit_exit(self.handle, "EOF")
            return
        code = self._proc.wait()
        if self._stopped.is_set() and code in (0, -signal.SIGINT):
            self._emit_exit(self.handle, "EOF")
            return
        if code == 0:
            self._emit_exit(self.handle, "EOF")
            return
        self._emit_exit(self.handle, f"tmux process exited: {code}")


def start_tmux_control_session(
    params: Dict[str, Any],
    emit_output,
    emit_exit,
    relay_origin_opt: str = RELAY_ORIGIN_PANE_OPT,
) -> TmuxControlSession:
    target, allow_nested = parse_tmux_start_command(params.get("command") or [])
    if not allow_nested:
        try:
            origin = pane_option_value(target, relay_origin_opt)
        except Exception as err:  # noqa: BLE001
            raise RPCError(
                ERR_INVALID_PARAMS,
                f"failed to inspect pane metadata for {target}: {err}",
            ) from err
        if origin:
            raise RPCError(
                ERR_INVALID_PARAMS,
                f"pane {target} is marked as relay-managed ({origin}); use --allow-nested to override",
            )

    session = TmuxControlSession(target, emit_output, emit_exit)
    session.start(int(params.get("rows") or 24), int(params.get("cols") or 80))
    return session
