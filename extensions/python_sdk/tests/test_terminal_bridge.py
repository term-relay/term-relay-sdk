from __future__ import annotations

import unittest
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python_sdk.bridge import (
    TerminalBridgeRuntime,
    TerminalBridgeStart,
    parse_terminal_bridge_start,
    start_terminal_bridge_session,
)
from python_sdk.sdk import RPCError


class FakeTransport:
    def __init__(self, ready: Optional[tuple[int, int]] = None):
        self.ready = ready
        self.connected: Optional[TerminalBridgeStart] = None
        self.inputs: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self.closed = 0
        self._on_output = lambda _: None
        self._on_exit = lambda _: None

    def set_event_handlers(self, on_output, on_exit):
        self._on_output = on_output
        self._on_exit = on_exit

    def connect(self, start: TerminalBridgeStart):
        self.connected = start
        return self.ready

    def send_input(self, data: bytes):
        self.inputs.append(data)

    def send_resize(self, rows: int, cols: int):
        self.resizes.append((rows, cols))

    def close(self):
        self.closed += 1

    def emit_output(self, data: bytes):
        self._on_output(data)

    def emit_exit(self, reason: str):
        self._on_exit(reason)


class ParseTerminalBridgeStartTests(unittest.TestCase):
    def test_parse_uses_explicit_target_when_present(self):
        start = parse_terminal_bridge_start(
            {"target": "pane://123", "command": ["fallback"], "rows": 30, "cols": 90}
        )
        self.assertEqual("pane://123", start.target)
        self.assertEqual(["fallback"], start.command)
        self.assertEqual(30, start.rows)
        self.assertEqual(90, start.cols)

    def test_parse_falls_back_to_command_first_arg(self):
        start = parse_terminal_bridge_start({"command": ["pane://abc"]})
        self.assertEqual("pane://abc", start.target)
        self.assertEqual(24, start.rows)
        self.assertEqual(80, start.cols)
        self.assertEqual("xterm-256color", start.term)

    def test_parse_requires_target(self):
        with self.assertRaises(RPCError):
            parse_terminal_bridge_start({})


class TerminalBridgeRuntimeTests(unittest.TestCase):
    def test_runtime_forwards_io_and_resize(self):
        outputs: list[tuple[str, bytes]] = []
        exits: list[tuple[str, str]] = []
        transport = FakeTransport(ready=(31, 101))
        start = TerminalBridgeStart(
            target="pane://42",
            command=["pane://42"],
            rows=24,
            cols=80,
            term="xterm-256color",
        )

        runtime = TerminalBridgeRuntime(
            transport=transport,
            start=start,
            emit_output=lambda handle, data: outputs.append((handle, data)),
            emit_exit=lambda handle, reason: exits.append((handle, reason)),
        )

        self.assertEqual("pane://42", runtime.target)
        self.assertEqual(31, runtime.rows)
        self.assertEqual(101, runtime.cols)
        self.assertIsNotNone(transport.connected)

        runtime.write_input(b"abc")
        runtime.resize(40, 120)

        self.assertEqual([b"abc"], transport.inputs)
        self.assertEqual([(40, 120)], transport.resizes)
        self.assertEqual(40, runtime.rows)
        self.assertEqual(120, runtime.cols)

        transport.emit_output(b"hello")
        self.assertEqual(1, len(outputs))
        self.assertEqual(b"hello", outputs[0][1])

        transport.emit_exit("done")
        transport.emit_exit("duplicate")
        self.assertEqual(1, len(exits))
        self.assertEqual("done", exits[0][1])

        runtime.stop()
        runtime.stop()
        self.assertEqual(1, transport.closed)

    def test_stop_blocks_late_output_forwarding(self):
        outputs: list[tuple[str, bytes]] = []
        transport = FakeTransport()
        start = TerminalBridgeStart(
            target="pane://7",
            command=["pane://7"],
            rows=24,
            cols=80,
            term="xterm-256color",
        )
        runtime = TerminalBridgeRuntime(
            transport=transport,
            start=start,
            emit_output=lambda handle, data: outputs.append((handle, data)),
            emit_exit=lambda _handle, _reason: None,
        )

        runtime.stop()
        transport.emit_output(b"ignored")
        self.assertEqual([], outputs)

    def test_start_terminal_bridge_session_uses_factory(self):
        captured: list[TerminalBridgeStart] = []
        transport = FakeTransport()

        def factory(start: TerminalBridgeStart):
            captured.append(start)
            return transport

        runtime = start_terminal_bridge_session(
            {"command": ["pane://xyz"], "rows": 33, "cols": 77},
            emit_output=lambda _handle, _data: None,
            emit_exit=lambda _handle, _reason: None,
            transport_factory=factory,
        )

        self.assertEqual(1, len(captured))
        self.assertEqual("pane://xyz", captured[0].target)
        self.assertEqual(33, runtime.rows)
        self.assertEqual(77, runtime.cols)


if __name__ == "__main__":
    unittest.main()
