# tmux Adapter for Control Protocol

This document specifies how tmux control-mode signals are translated into the
canonical control protocol defined in `docs/control_protocol.md`.

## Purpose

tmux does not emit an explicit "control owner changed" event. It emits state
signals such as `%layout-change` and `%output`. The adapter must convert these
signals into protocol-level control actions.

## Native tmux Signals (Control Mode)

- `%layout-change window-id ...`
  - Layout/visible-layout changed.
  - Often indicates another client size/activity became effective.
- `%output pane-id value`
  - Pane produced output.
- `%session-changed ...`, `%exit`, etc.
  - Session lifecycle notifications.

## Mapping: tmux -> Control Protocol

### Local reclaim trigger

- On `%layout-change`:
  - Adapter marks local size cache stale.
  - If currently connected and protocol controller is not `local`,
    send `control_request` to Hub with cooldown.
  - Rationale: emulate tmux "active client drives size" behavior with an
    explicit protocol claim.

### Input-time reclaim

- Web side (same protocol domain) also reclaims on input:
  - If `controller_id != subscriber_id`, send `control_request` before input
    (cooldown-protected).
  - This keeps behavior symmetric with "typing implies active control intent".

### Layout fallback capture (display consistency)

- `%layout-change` is not always sufficient for full repaint delivery.
- Adapter starts a short delay window:
  - if target-pane `%output` arrives soon, skip forced capture.
  - if not, perform one fallback `capture-pane`.
- Apply capture throttle to avoid repeated large snapshots.

## Mapping: Control Protocol -> tmux

### `control {controller_id}`

- Update adapter controller state.
- `controller_id` is advisory metadata for ownership.
- Resize application is state-driven by incoming `resize` frames.

### `resize {rows, cols}`

- Apply incoming size updates to tmux client.
- Use `refresh-client -C <cols>x<rows>`.
- Do not issue `resize-window` or `set-window-option ... window-size ...` in
  runtime handoff paths; those can force sticky window-size modes and break
  multi-attacher parity.

## History/Viewport Compatibility

tmux viewport and Web viewport may differ. Adapter behavior:

- Initial snapshot:
  - normal screen: `capture-pane -p -e -S - -E -` (full history).
  - alternate screen: `capture-pane -p -e -a`.
- Runtime output:
  - `%output` remains the primary stream.
  - fallback capture only for layout-change gaps.

## Current Cooldown / Timing Policy

- Local reclaim request minimum interval: ~1s.
- Layout fallback capture delay: ~120ms.
- Layout fallback capture throttle: ~500ms.

These are adapter-level policies, not protocol-level requirements.

## Why This Adapter Exists

- tmux-native signals are implicit and backend-specific.
- Protocol messages are explicit and backend-agnostic.
- Adapter keeps system-wide semantics stable while preserving tmux UX.

## Sequence Diagrams

### 1) `%layout-change` -> Protocol Reclaim

```text
tmux Server          tmux Adapter               Hub                Web
    |                    |                       |                  |
    | %layout-change     |                       |                  |
    |------------------->|                       |                  |
    |                    | mark local stale      |                  |
    |                    | control_request       |                  |
    |                    |---------------------->|                  |
    |                    |                       | control{local}   |
    |                    |<----------------------|----------------->|
    |                    | apply local size mode |                  |
    |                    |                       |                  |
```

### 2) `%layout-change` with Output vs Fallback Capture

```text
tmux Server          tmux Adapter                           Hub/Web
    |                    |                                      |
    | %layout-change     |                                      |
    |------------------->| start 120ms fallback timer           |
    |                    |------------------------------------->|
    | %output            |                                      |
    |------------------->| cancel fallback capture              |
    |                    | send output offset stream            |
    |                    |------------------------------------->|
```

```text
tmux Server          tmux Adapter                           Hub/Web
    |                    |                                      |
    | %layout-change     |                                      |
    |------------------->| start 120ms fallback timer           |
    |                    |------------------------------------->|
    | (no output)        |                                      |
    |                    | timer fires                          |
    |                    | capture-pane fallback                |
    |                    | send captured frame                  |
    |                    |------------------------------------->|
```

### 3) Web Input Reclaim Complements tmux Reclaim

```text
Web                  Hub                    tmux Adapter
 |                    |                          |
 | input intent       |                          |
 | (not controller)   |                          |
 | control_request    |                          |
 |------------------->|                          |
 |                    | control{web}             |
 |<-------------------|------------------------->|
 | input/resize       |                          |
 |------------------->|------------------------->|
```
