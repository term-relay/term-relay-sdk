# Control Protocol Spec

This document defines the canonical size-control protocol for Term Relay.

The protocol is backend-agnostic. tmux, iTerm2 pane takeover, shell plugins, or
future injectors should map their native signals into this protocol via adapters.

## Goals

- Keep one authoritative controller state across CLI, Hub, and Web.
- Decouple "who controls size" from "who can input".
- Make backend integration predictable by standardizing claim/release semantics.

## Non-Goals

- Input arbitration. Input is shared by design.
- Backend-specific window internals (those belong in adapter docs).

## Entities

- Hub: authority for `controller_id` state.
- Session runner (CLI backend adapter): applies size according to controller.
- Subscriber (Web client): may request/release control and send resize.
- Local controller id: literal `local`.

## Messages

### Client -> Hub

- `control_request`
  - Meaning: "set controller to this subscriber".
- `control_release`
  - Meaning: "release to local controller".
- `resize {rows, cols}`
  - Meaning: "apply these dimensions if I am current controller".

### Hub -> CLI / Subscribers

- `control {controller_id}`
  - Broadcast on every controller change.

### Subscribe Handshake

- `subscribed {subscriber_id, controller_id, ...}`
  - Initial controller state for new subscribers.

## Core Semantics

1. Controller state is authoritative at Hub.
2. Input remains shared regardless of controller.
3. Resize is controller-gated.
4. Local controller id is `local`.
5. On new session creation, controller defaults to `local`.

## Arbitration Rules

- `control_request`:
  - Current implementation: set controller to requesting subscriber immediately.
  - Concurrent claims resolve by server processing order (last processed wins).
- `control_release`:
  - Current implementation: set controller to `local`.
- Subscriber disconnect:
  - If disconnected subscriber was controller, fallback to `local`.

## Resize Rules

- Hub forwards `resize` only when sender is current controller.
- Runner applies size when controller is remote subscriber.
- Runner ignores remote resize when controller is `local`.

## Reliability Notes

- Clients should rate-limit repeated `control_request` to avoid flapping.
- Adapters should avoid emitting claim spam on noisy backend signals.

## State Invariants

- `controller_id` is always either `local` or a currently known subscriber id.
- Control changes do not block input forwarding.
- Controller handoff should be followed by an effective size apply.

## Adapter Contract

Every backend adapter must provide:

1. **Inbound mapping**: native backend events -> `control_request`/`control_release`.
2. **Outbound mapping**: protocol controller/resize decisions -> backend-native
   size/state updates.
3. **Cooldown policy**: prevent noisy oscillation from raw native events.

For tmux-specific mapping, see:

- `docs/control_protocol_tmux_adapter.md`

## Sequence Diagrams

### 1) Claim Control (Web Takes Size)

```text
Web Subscriber         Hub                    Runner Adapter
     |                  |                           |
     | control_request  |                           |
     |----------------->|                           |
     |                  | set controller=subscriber |
     |                  |-------------------------->|
     |                  | control{subscriber}       |
     |<-----------------|<--------------------------|
     |                  |                           |
     | resize{r,c}      |                           |
     |----------------->|                           |
     |                  | resize{r,c}               |
     |                  |-------------------------->|
     |                  |                           |
```

### 2) Release Control (Back to Local)

```text
Web Subscriber         Hub                    Runner Adapter
     |                  |                           |
     | control_release  |                           |
     |----------------->|                           |
     |                  | set controller=local      |
     |                  |-------------------------->|
     |                  | control{local}            |
     |<-----------------|<--------------------------|
     |                  |                           |
```

### 3) Input-Time Reclaim (Not Controller, User Types)

```text
Web Subscriber         Hub                    Runner Adapter
     |                  |                           |
     | (sees controller != self)                    |
     | control_request  |                           |
     |----------------->|                           |
     |                  | control{self}             |
     |<-----------------|-------------------------->|
     | input{data}      |                           |
     |----------------->| input{data}               |
     |                  |-------------------------->|
     | resize{r,c}      |                           |
     |----------------->| resize{r,c}               |
     |                  |-------------------------->|
```
