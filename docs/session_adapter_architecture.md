# Session Adapter Architecture

This document defines the unified, extensible architecture for sharing terminal
sessions across different backends and platforms.

It consolidates current practice and future direction for:
- Start-and-share flows (new process sessions)
- Attach-and-share flows (existing sessions, for example tmux)
- Future takeover flows (iTerm2 pane takeover, terminal plugins, OS-level injection)

## Why This Doc

Current design assets are strong but split across:
- `docs/control_protocol.md` (protocol contract)
- `docs/control_protocol_tmux_adapter.md` (tmux translation)
- `term-relay-cli/docs/tmux_integration.md` (tmux behavior and validation)
- `term-relay-iterm2/docs/iterm2_plugin_and_extension_sdk.md` (iTerm2 plugin + bridge design)
- `term-relay-cli/e2e_cli_tmux_web_test.go` (tmux+web interop coverage)
- `term-relay-iterm2/iterm2-plugin/*` (prototype implementation path)

What is missing is one top-level architecture for all session source types.

## Design Goals

- Keep one backend-agnostic session/share model.
- Reuse the same Hub protocol and Web behavior across adapters.
- Allow new adapters without changing Hub core semantics.
- Preserve tmux-like multi-view size behavior where possible.
- Define conformance tests adapters must pass before production use.

## Non-Goals

- Defining terminal emulator rendering details in Web.
- Replacing backend-native control semantics with a fake universal terminal API.
- Solving every takeover backend up front.

## Terminology

- Session source: the runtime process or terminal surface being shared.
- Adapter: translation layer between a backend-native session source and Term Relay protocol.
- Runner: the relay process that connects to Hub and streams I/O.
- Controller: size authority (`local` or subscriber id); input remains shared.

## Current State (Implemented)

### Mode A: Start-and-share

- Entry: `term-relay <command ...>` or `./dev/term-relay.sh ...`
- Source: new PTY child process
- Status: implemented and e2e-covered

### Mode B: Attach-and-share (tmux)

- Entry: `term-relay-tmux share <target>`
- Source: existing tmux pane via control mode (`tmux -C attach`)
- Status: implemented and e2e-covered

### Mode C: Takeover (future)

- Example targets: iTerm2 live session, terminal plugins, Linux interception
- Status: design/prototype only (iTerm2 plugin exists, not integrated into common adapter framework)

## Unified Adapter Model

Every integration backend should implement the same conceptual adapter contract.

### 1) Discovery

- Enumerate attachable targets (if backend supports attach/takeover).
- Return stable target identity and metadata:
  - id
  - source type (`spawn`, `attach`, `takeover`)
  - dimensions (if known)
  - command/title/session metadata

### 2) Session Binding

- Resolve user-selected target into a concrete source handle.
- Validate target availability and permissions.
- Apply nested-share guard if source is already relay-managed.

### 3) Streaming

- Output stream: native output -> relay output offsets.
- Input stream: relay input -> native source input.
- Resize stream: relay size decisions -> native resize operation.
- Snapshot path: full capture for initial sync and fallback resync.

### 4) Control Translation

- Translate native control/active-view signals into protocol semantics where needed.
- Respect canonical controller model from `docs/control_protocol.md`.
- Keep input shared regardless of controller.

### 5) Lifecycle

- Start: connect and begin streaming.
- Stop: graceful detach/restore, then force-kill fallback.
- Recover: reconnect and rebuild session state if backend allows.

## Capability Flags

Adapters should publish capabilities so upper layers can branch safely.

Suggested capability fields:
- `can_spawn`
- `can_attach`
- `can_takeover`
- `can_list_targets`
- `has_history_snapshot`
- `has_native_layout_events`
- `supports_shared_input`
- `supports_controller_resize`
- `supports_restore_on_stop`

These capabilities are for runtime behavior and test gating, not for protocol changes.

## Adapter Types

### SpawnAdapter

- Creates a new source process and shares immediately.
- Primary implementation today: PTY-backed CLI runner.

### AttachAdapter

- Attaches to a pre-existing source and shares.
- Primary implementation today: tmux control-mode adapter.

### TakeoverAdapter

- Takes control of an already-running terminal surface.
- Examples: iTerm2 session manager, plugin-based terminal integration.

## Mapping to Existing Implementation

- Adapter boundary:
  - `internal/sessionadapter/adapter.go`
  - `internal/sessionadapter/spawn.go`
  - `internal/sessionadapter/tmux_attach.go`
  - `internal/sessionadapter/takeover_mock.go`
- Spawn path:
  - `main.go`
  - `internal/pty/*`
  - `internal/relay/cloud_relay.go`
  - `term-relay --adapters` / `term-relay --adapters-json` exposes catalog
  - `term-relay --list-targets --targets-backend tmux [--targets-json]` exposes backend target discovery from the root CLI
- tmux attach path:
  - `cmd/term-relay-tmux/main.go`
  - `internal/tmux/control.go`
  - `internal/tmux/control_relay.go`
  - `term-relay-tmux list` uses adapter discovery (`ListTargets`)
  - `term-relay-tmux adapters` surfaces capability flags for attach/takeover candidates
- Shared control semantics:
  - `docs/control_protocol.md`
  - `hub/internal/router/router.go`
  - `hub/web/terminal_policy.js`

## Conformance Test Plan

All adapters should pass a common behavior matrix.

Unified local gate command:
- `./dev/e2e-conformance.sh`

### A. No-Hub Adapter Sanity

- startup/shutdown behavior
- local status and cleanup behavior (if backend has UI state)
- nested-share guard
- non-interactive execution behavior

Current examples:
- `e2e_dev_tmux_script_test.go`

### B. Hub Interop

- device bind + session visibility
- output/input roundtrip
- subscribe/resync correctness
- reconnect behavior

Current examples:
- `e2e_phase2_test.go`
- `e2e_reconnect_mock_test.go`

### C. Multi-Controller Size Handoff

- multiple local views + web
- repeated controller handoff
- history/snapshot integrity under layout changes

Current examples:
- `e2e_size_handoff_test.go`
- `e2e_cli_tmux_web_test.go`

## Scenario-to-Test Mapping (Current Gates)

This maps product acceptance scenarios to automated e2e coverage:

1. Device binding with CLI and Web:
  - bind flow + API confirm + session visibility
  - `e2e_cli_tmux_web_test.go` (`TestE2ECLIAndTmuxWebInterop`)
2. Start new shell then share:
  - `dev/term-relay.sh` share path
  - `e2e_cli_tmux_web_test.go` (`cli_share_*` subtests)
3. Share existing tmux session:
  - attach existing pane then Web I/O
  - `e2e_cli_tmux_web_test.go` (`tmux_attach_existing_session_share_and_web_interop`)
4. Create new tmux session then share:
  - new tmux session creation + immediate sharing
  - `e2e_cli_tmux_web_test.go` (`tmux_new_session_share_and_web_interop`)
5. Size handoff consistency:
  - tmux-only handoff, tmux+web handoff, and non-tmux handoff
  - `e2e_size_handoff_test.go`

## Platform Expansion Plan

### Phase 1 (done/active)

- Stabilize SpawnAdapter and tmux AttachAdapter behavior.
- Keep protocol and e2e as source of truth.

### Phase 2

- Extract explicit adapter package boundary:
  - source discovery
  - attach/spawn lifecycle
  - stream handlers
  - capabilities
- Refactor tmux and spawn paths to implement the same internal interface.

### Phase 3

- Integrate iTerm2 plugin path as TakeoverAdapter candidate.
- Add adapter-specific translation doc:
  - `docs/control_protocol_iterm_adapter.md` (future)

### Phase 4

- Add Linux takeover candidates (PTY interception/injection).
- Add per-platform adapter conformance suites.

## Risks and Constraints

- Native backends differ in what "active view" and "size ownership" mean.
- Some backends may not provide reliable layout-change notifications.
- Snapshot cost can be high under rapid layout churn.
- Takeover backends can have stronger security/permission constraints.

## Decision Rule

When backend semantics conflict, protocol invariants win:
- input is shared
- controller governs resize application
- session state must remain reconnect-safe

Backend-specific UX behavior should be preserved only if it does not violate
these invariants.
