#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from python_sdk import SingleSessionRPCServer, build_hello, start_tmux_control_session  # noqa: E402


def main() -> int:
    hello = build_hello(
        ext_id="com.termrelay.python.tmux",
        name="Term Relay Python Tmux Extension",
        version="0.1.0",
        capabilities={
            "can_spawn": True,
            "can_attach": True,
            "can_takeover": False,
            "can_list_targets": False,
            "has_history_snapshot": True,
            "has_native_layout_events": True,
            "supports_shared_input": True,
            "supports_controller_resize": True,
            "supports_restore_on_stop": True,
        },
    )
    return SingleSessionRPCServer(
        hello_payload=hello,
        start_session=start_tmux_control_session,
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
