# Term-Relay 多仓规划（仓库拓扑与职责）

## 1. 设计原则

1. 商业核心闭源，生态接口开源。  
2. Hub/CLI 的发布节奏可控，不被 SDK 社区节奏绑定。  
3. SDK 与插件可独立版本化、独立发布。  
4. 对外开源仓保持“可运行、可贡献、可复现”。

## 2. 推荐仓库清单（目标态）

## 2.1 私有仓库

### A. `term-relay-platform`（私有）

- 角色：产品核心后端与控制台
- 主要内容：
  - Hub 服务端（会话路由、鉴权、配额、账单状态）
  - Web 控制台（设备、会话、套餐、账单 UI）
  - 支付回调、订阅状态机、配额控制
- 发布物：
  - SaaS 后端镜像
  - 内部管理端

### B. `term-relay-cli`（私有）

- 角色：闭源 CLI 与本地 runtime
- 主要内容：
  - CLI 主程序
  - 本地 relay/runtime
  - extension host（闭源 runtime 实现）
  - 官方打包与签名脚本
- 发布物：
  - macOS/Linux CLI 二进制

### C. `term-relay-ops`（私有）

- 角色：部署与运维
- 主要内容：
  - IaC（Terraform/Helm/K8s）
  - 环境配置模板
  - 发布流水线与回滚脚本
  - 监控告警配置

## 2.2 开源仓库

### D. `term-relay-sdk`（公开）

- 角色：开放 SDK 与协议规范
- 主要内容：
  - `rpc-v1` / `simple-io-v1` 协议文档
  - `extensions/python_sdk/**`
  - extension manifest 示例（spawn/tmux/iTerm2）
  - 扩展模板与示例测试
- 发布物：
  - SDK 版本（SemVer）
  - 文档站 SDK 部分

### E. `term-relay-iterm2`（公开）

- 角色：iTerm2 官方插件/桥接实现（开源）
- 主要内容：
  - iTerm2 bridge（socket 协议）
  - iTerm2 plugin 示例
  - `iterm2-relay list/share` 开发工具
- 发布物：
  - iTerm2 集成安装包/脚本
  - 示例与 FAQ

### F. `term-relay-examples`（公开，可选）

- 角色：社区扩展示例集合
- 主要内容：
  - 非官方但可运行的扩展示例
  - 各语言最小模板（Python/Node/Go/Rust）

## 3. 当前目录到目标仓库映射

| 当前路径 | 目标仓库 |
|---|---|
| `hub/**` | `term-relay-platform` |
| `main.go`、`internal/**`（闭源 runtime 部分） | `term-relay-cli` |
| `cmd/term-relay*`（闭源命令） | `term-relay-cli` |
| `extensions/python_sdk/**` | `term-relay-sdk` |
| `extensions/*/term-relay.extension.json` | `term-relay-sdk` |
| `extensions/python-iterm2/**` | `term-relay-iterm2` |
| `iterm2-plugin/**` | `term-relay-iterm2` |
| `dev/iterm2-bridge*`、`dev/iterm2-relay.sh` | `term-relay-iterm2` |
| 协议与扩展文档 | `term-relay-sdk` / `term-relay-iterm2`（按主题拆） |
| 部署脚本/环境配置 | `term-relay-ops` |

## 4. 开发协作模型

## 4.1 Source of Truth（推荐）

建议采用“私有主线 + 自动导出开源仓”：

1. 日常开发在私有主线仓完成。  
2. 通过目录级同步（subtree/filter-repo/脚本）导出到开源仓。  
3. 开源仓版本由同步流水线生成 tag/release。  

好处：

- 防止闭源代码误公开
- 统一 CI 与测试基线
- 便于做跨模块重构

## 4.2 外部贡献回流

建议流程：

1. 外部贡献先在开源仓提 PR。  
2. 维护者审核后手工/机器人回灌到私有主线。  
3. 再次同步导出，保证历史一致。  

## 5. 版本与发布节奏

| 仓库 | 版本策略 | 节奏建议 |
|---|---|---|
| `term-relay-platform` | 内部版本 + API 版本号 | 每周/双周 |
| `term-relay-cli` | SemVer（闭源发行） | 每周/双周 |
| `term-relay-sdk` | SemVer（公开） | 每月或按特性 |
| `term-relay-iterm2` | SemVer（公开） | 每月或按特性 |

兼容性建议：

- Hub 与 CLI：同主版本兼容（例如 `1.x`）  
- SDK 与 CLI：协议版本兼容（`v1`）优先于实现版本  

## 6. 权限与安全建议

1. 私有仓强制 CODEOWNERS + 必须评审。  
2. 开源仓启用安全扫描、依赖更新机器人。  
3. 同步流水线使用最小权限 token。  
4. 明确哪些目录永不导出（鉴权、支付、运维、内部配置）。  

