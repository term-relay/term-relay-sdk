# Python Extension SDK (MVP)

Shared helpers for Python extensions:

- `SimpleIOServer`: runs `simple-io-v1` JSONL loop (`start/input/resize/stop`)
- `PTYSimpleIOAdapter`: reusable PTY-backed simple-io adapter
- `JsonRPCServer`: runs `rpc-v1` JSON-RPC loop with notification support
- `SingleSessionRPCServer`: reusable `ext.start/input/resize/stop` single-session RPC wiring
- `start_tmux_control_session`: reusable tmux control-mode session bridge
- `TerminalBridgeRuntime`: reusable generic terminal-bridge runtime
- `start_terminal_bridge_session`: parse + create a bridge runtime from start params
- `Iterm2SocketTransport` / `start_iterm2_socket_session`: iTerm2 bridge socket skeleton
- `list_iterm2_targets`: query bridge `list_targets` JSONL API
- `encode_b64` / `decode_b64`
- `RPCError`

Current consumers:

- `extensions/python-spawn/main.py`
- `extensions/python-tmux/main.py`

Design goal: extension implementations only keep backend logic (PTY/tmux
bridging), while protocol parsing and framing stays in one place.

## Testing

SDK-level runtime tests (no Hub dependency):

```bash
python3 -m unittest discover -s extensions/python_sdk/tests -p 'test_*.py' -v
```

## Integrating Custom Terminal Backends

Use `start_terminal_bridge_session` with a custom transport implementation.
Only these methods are required:

- `set_event_handlers(on_output, on_exit)`
- `connect(start)` (can return negotiated `(rows, cols)`)
- `send_input(data)`
- `send_resize(rows, cols)`
- `close()`

This lets iTerm plugin bridges, Linux injection agents, or other terminal
connectors reuse the same SDK protocol loop without rewriting JSONL/RPC framing.
