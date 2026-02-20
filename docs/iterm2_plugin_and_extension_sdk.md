# iTerm2 Plugin 与通用扩展机制设计

## 1. 背景与目标

当前仓库已经有：
- 内建 `spawn` 路径（`term-relay <cmd>`）
- 内建 `tmux attach` 路径（`term-relay-tmux share`）
- adapter 抽象（`internal/sessionadapter/*`）

但要支持更多终端（iTerm2、其他终端插件、系统注入等），如果每个都写进 core，会导致：
- 迭代慢
- 维护成本高
- 社区难以贡献

本设计目标：
- 给 iTerm2 一条可用、稳定、可热更新的 plugin 路径。
- 定义一个统一扩展接口，让社区作者用少量代码就能接入新 backend。
- 让 `tmux-proxy`/`term-relay-tmux`/未来 backend 都走同一套扩展协议，而不是各写一套。

## 2. 关键决策

### D1. iTerm2 采用 plugin 路线（不是纯 osascript 轮询）

原因：
- 非侵入方式很难拿到高质量实时输出与终端事件。
- plugin 能直接获得 session 事件和控制能力，语义更完整。

### D2. plugin 必须支持热加载/热更新，不要求重启 iTerm2

策略：
- AutoLaunch 只放“极小 bootstrap”。
- 业务逻辑放版本目录，bootstrap 动态加载当前版本。
- 用 `it2run` 触发首次加载（无需重启 iTerm2）。
- 更新时切换 `current` 符号链接 + 触发 `reload` RPC。

### D3. 扩展机制采用“进程级协议 + adapter.type”

v1 支持两种并存协议（保留旧实现，允许更简单新实现）：
- `adapter.type=rpc-v1`（默认）：JSON-RPC over stdio，功能最完整。
- `adapter.type=simple-io-v1`：line-framed JSON over stdio，只处理会话
  `start/input/resize/output/exit` 映射，扩展实现成本最低。

原因：
- 跨语言简单（Go/Python/Node/Rust/Shell 都易实现）。
- 不绑定特定运行时。
- 便于 core 做监管、超时、重启和权限控制。
- 能按 backend 复杂度选择协议，避免“所有扩展都必须实现完整 RPC”。

## 3. 总体架构

```text
+--------------------------+       +-----------------------+
| term-relay core          |       | Hub                   |
|  - adapter facade        |<----->| ws routing/session    |
|  - extension host        |       +-----------------------+
|  - conformance runner    |
+-----------+--------------+
            |
            | JSON-RPC over stdio
            v
+--------------------------+
| Extension Process        |
|  (tmux/iterm2/others)    |
|  - list targets          |
|  - attach/takeover       |
|  - stream output/input   |
|  - resize/snapshot       |
+--------------------------+
```

## 4. Extension SDK v1（最小接口）

### 4.1 Manifest（`term-relay.extension.json`）

```json
{
  "id": "com.termrelay.iterm2",
  "name": "iTerm2 Extension",
  "version": "0.1.0",
  "protocol_version": "v1",
  "adapter": { "type": "rpc-v1" },
  "entry": "./bin/term-relay-ext-iterm2",
  "capabilities": {
    "can_spawn": false,
    "can_attach": false,
    "can_takeover": true,
    "can_list_targets": true,
    "has_history_snapshot": true,
    "has_native_layout_events": true,
    "supports_shared_input": true,
    "supports_controller_resize": true,
    "supports_restore_on_stop": true
  }
}
```

说明：
- `adapter.type` 省略时默认 `rpc-v1`（向后兼容）。
- `attach-only` 扩展当前要求 `rpc-v1`（tmux attach 依赖完整能力）。

### 4.2 `rpc-v1`: Host -> Extension RPC

- `ext.hello() -> {id, version, capabilities}`
- `ext.list_targets({filter}) -> {targets[]}`
- `ext.start({target_id, hub_url, auth, options}) -> {session_handle}`
- `ext.input({session_handle, data_b64})`
- `ext.resize({session_handle, rows, cols})`
- `ext.capture({session_handle, mode}) -> {data_b64, rows, cols}`
- `ext.stop({session_handle})`
- `ext.health() -> {ok, details}`

### 4.3 `rpc-v1`: Extension -> Host 事件

- `event.output {session_handle, data_b64}`
- `event.layout_change {session_handle, rows, cols, source}`
- `event.exit {session_handle, reason}`
- `event.log {level, message}`

### 4.4 `simple-io-v1`: line-framed JSON 事件

Host -> Extension：
- `{"type":"start","command":[...],"rows":24,"cols":80,"term":"xterm-256color"}`
- `{"type":"input","data_b64":"..."}`
- `{"type":"resize","rows":35,"cols":71}`
- `{"type":"stop"}`

Extension -> Host：
- `{"type":"ready","rows":24,"cols":80}`（可选）
- `{"type":"output","data_b64":"..."}`
- `{"type":"exit","reason":"EOF"}`

目标：
- 让扩展作者只关注本地终端动作翻译，不需要实现完整 RPC 方法集。
- 保持 Hub/Auth/control 逻辑都在 launcher/core。

### 4.5 协议不变式

- 输入始终共享（不做输入互斥）。
- 控制权只影响 resize 策略。
- 扩展是“后端翻译层”，不能改写 Hub 核心语义。

## 5. iTerm2 Plugin 设计（热更新版）

## 5.1 目录布局

```text
~/.term-relay/iterm2/
  bootstrap/
    bootstrap.py
  releases/
    0.1.0/
      plugin_main.py
      handlers.py
      ...
    0.1.1/
      ...
  current -> releases/0.1.1
  runtime/
    state.json
    plugin.sock (optional)
```

AutoLaunch 中仅保留稳定 bootstrap（不承载业务逻辑）。

### 5.2 启动与首次加载

1. 安装 bootstrap 到 iTerm2 AutoLaunch。
2. 用 `it2run <bootstrap.py>` 立即启动（无需重启 iTerm2）。
3. bootstrap 读取 `current`，动态加载业务模块并注册 RPC。

### 5.3 热更新流程

1. 下载新版本到 `releases/<new>`.
2. 原子切换 `current` 符号链接。
3. 调用 bootstrap 暴露的 `term_relay_reload()` RPC：
   - 停旧任务（流、心跳、回调）
   - `importlib.reload(...)`
   - 重新注册 RPC/事件处理
4. reload 失败时自动回滚到上一版本。

### 5.4 关键 RPC（iTerm2 内部）

- `term_relay_toggle_share(session_id)`
- `term_relay_list_sessions()`
- `term_relay_reload()`
- `term_relay_status()`

## 6. 与现有 tmux/spawn 的统一方式

目标不是推翻现有实现，而是逐步收敛：

### 阶段 A（并行）
- 保留现有内建 `spawn`、`tmux attach`。
- 同时实现 extension host 与 SDK。

### 阶段 B（双通路）
- 增加 `tmux` 扩展版（参考实现），与内建版并存。
- CI conformance 同时跑内建与扩展路径。

### 阶段 C（收敛）
- 核心入口默认走扩展 host。
- 内建实现降级为 fallback 或测试基线。

## 7. 社区扩展开发模型

为降低门槛，提供：
- `term-relay ext init`：生成扩展模板（manifest + RPC skeleton）。
- `term-relay ext validate`：本地协议校验。
- `term-relay ext test`：跑标准 conformance 子集。
- 文档模板：最小实现只需支持 `list_targets + start + input + output + stop`。

社区扩展只需完成“终端事件 <-> SDK 事件”翻译层，不需要理解 Hub 内部细节。

## 8. 安全与隔离

- 扩展默认低信任：独立进程 + 超时 + 心跳。
- Host 维护 allowlist（可配置）。
- 认证令牌仅由 host 注入，扩展不直接读全局配置。
- 扩展崩溃不应影响 core 主进程；由 host 执行重启/隔离。

## 9. Conformance 要求（扩展必须通过）

基础：
- 可枚举目标
- 可启动/停止共享
- 输入输出 roundtrip
- 断连恢复不崩溃

可选增强：
- resize handoff
- history snapshot
- layout-change 语义一致性

建议新增：
- `e2e_extension_conformance_test.go`

## 10. 里程碑建议

### M1（本阶段）
- 文档定稿（本文件）
- Extension SDK v1 定义 + host 骨架
- 新版最小 launcher（基于扩展机制）:
  - `cmd/term-relay-ext/main.go`
  - `dev/term-relay-ext.sh`
- spawn 扩展参考实现（不含 Hub/Auth 逻辑）:
  - `cmd/term-relay-ext-spawn/main.go`
  - `extensions/spawn/term-relay.extension.json`
- 关键约束：Auth/Hub/control 均在 launcher，扩展只负责本地终端动作。

### M2
- iTerm2 takeover 最小可用（输出/输入/基础 resize）
- `--targets-backend iterm2` 与 adapter catalog 打通

### M3
- tmux 扩展参考实现
- 扩展模板/校验工具发布
- 社区贡献指南

## 11. 未决问题

- RPC 传输是否需要从 stdio 升级到 unix socket（仅性能优化，不影响 v1 语义）。
- iTerm2 多 session 并发时的 backpressure 策略。
- Windows 终端扩展（WT/ConPTY）是否复用同一 SDK 事件集。
