# Cocodex 开发者文档

本文面向 Cocodex 项目的维护者，说明当前实现模型和关键状态机。用户使用流程请阅读根目录的 [README.md](../README.md) 或 [中文 README](README_ZH.md)。

## 架构

Cocodex 是围绕 Git 和 Codex 构建的单机协作编排层，主要由以下部分组成：

- CLI：`src/cocodex/cli.py`。
- 持久状态：`src/cocodex/state.py`，使用 `.cocodex/state.sqlite` 中的 SQLite。
- Daemon 编排：`src/cocodex/daemon.py`。
- Session 侧协作 agent：`src/cocodex/agent.py`。
- Session worktree 初始化：`src/cocodex/session.py`，包括为 Codex 生成 Cocodex 指导文件和配置 per-worktree Git identity。
- Main branch 保护：`src/cocodex/guard.py`。

daemon 和 session agent 通过 Unix domain socket 传输 JSONL 消息。Git 操作通过 `src/cocodex/git.py` 中的 helper 调用 Git CLI 完成。

`init` 和 daemon 启动都会在仓库 common hooks 目录安装 Cocodex 管理的 Git hooks。`reference-transaction` 会阻止普通 Git 命令更新 `refs/heads/<main>`，`pre-push` 会阻止直接 push `main`，pre-commit/rebase/merge hooks 会为常见命令提供更早的错误。Cocodex 自己写入 main 或执行 scoped remote push 时，通过 Git helper 设置 `COCODEX_INTERNAL_WRITE=1`。这能防止普通 Git CLI 误操作；它不能防止有人故意直接编辑 `.git` 文件。

daemon socket 通过配置的 `socket_path` 寻址。如果这个路径超过 Linux
`AF_UNIX` 限制，transport 会在配置路径写一个小的 pointer file，并把真正的 socket bind 到系统临时目录下。每个 session 的 control socket 也使用系统临时目录下的短路径，并用 repository hash 和 session name 区分。这样可以避免项目路径很深或位于 CI 临时目录时触发 path length 限制。

`SessionAgent` 可以把 sync prompt 粘贴到 tmux 中。`join` 会在环境里存在 `TMUX_PANE` 时默认使用当前 pane，这符合“开发者从自己的 tmux pane 中通过 Cocodex 启动 Codex”的产品约束。高级启动器可以用 `--tmux-target` 覆盖检测到的目标。收到 `start_fusion` 后，agent 总会在 task file 旁边写出 prompt file 并打印二者路径；如果有可用目标 pane，也会额外通过 `tmux load-buffer` 和 `paste-buffer` 把 prompt 放进 Codex 输入框。它不会发送 Enter；开发者需要看一下粘贴的 prompt，然后自己按 Enter 启动 task。生产路径要求 prompt 成功投递到 pane 后才接受 semantic task；测试 harness 可以设置 `COCODEX_HEADLESS_PROMPT_OK=1`，把写出 prompt file 视为已投递。

## 配置模型

`cocodex init` 通过 `src/cocodex/config.py` 中的 `init_config()` 写入 `.cocodex/config.json`。公开配置 schema 包含：

- `main_branch`：Cocodex 可以发布的本地主分支。
- `remote`：可选 remote 名，用于 best-effort scoped sync 本地 `main_branch` 和当前 session branch。
- `socket_path`：daemon Unix socket path。
- `worktree_root`：managed session worktree 根目录。
- `dirty_interval_s`：保留的 daemon polling 时间参数。
- `developers`：以 developer/session name 为 key 的对象。

每个 developer entry 必须提供 `git_user_name` 和 `git_user_email`，之后该开发者才能执行 `cocodex join <name>`。可选的 `command` 字段必须是非空 JSON 字符串数组，默认值是 `["codex"]`。`validate_config()` 会检查 remote 是否存在、main branch 是否存在、developer object 形状以及自定义 command 形状。identity 字段由 `join` 要求，而不是 daemon 启动时强制要求，因此 operator 可以逐步添加开发者。

`init_config()` 默认拒绝覆盖已有配置，除非 `cocodex init --force` 传入 `force=True`。配置写入使用临时文件加 atomic replace，避免写入失败后留下半截 JSON。
`init_config()` 还会把 `/.cocodex/` 加入 `.git/info/exclude`，并安装 main guard hooks。daemon 启动会重复这两个检查，因此旧仓库在启动新版 daemon 时也会被补齐。

`load_config()` 只接受上面列出的公开配置 schema。未知 key 会被明确报告为配置错误，避免过期或拼写错误的设置静默影响 session。Cocodex 不保存 repo-wide verification command：生成的 sync task 会要求拥有该任务的 Codex 为这次语义融合自行设计并执行合适验证。

## 产品命令模型

普通开发者命令是：

```bash
cocodex sync
```

这个命令在 managed worktree 中执行。CLI 会用当前 Git worktree root 匹配已注册的 `SessionRecord.worktree` 来推断 session。`sync` 刻意不接受 session name，这样开发者不会误触发其他用户 worktree 的同步。

内部实现中，`sync` 会根据状态映射到不同协议动作：

- 没有 active task 且没有本地更改：在可能时把 clean session catch up 到最新 `main`；
- 没有 active task、有本地更改且 `main == last_seen_main`：在 integration lock 保护下直接发布当前 session branch；
- 没有 active task、有本地更改且 `main != last_seen_main`：调用 `ready_to_integrate`；该请求要么通过 clean Git merge 直接发布，要么在同一次请求中启动 semantic task，要么带详细原因拒绝；
- active task 处于任何可恢复的同 session 状态：通过 `fusion_done` 报告当前 session `HEAD` 是 candidate；daemon 会在安全时归一化旧状态或中断状态，然后校验并发布，或者用同 session 下一步动作拒绝。

本地 catch-up、publish 或 task 启动成功后，CLI 会在配置了 `config.remote` 时尝试 best-effort remote sync。该操作会 force-push 本地 `main_branch`、当前 session branch 和 `refs/cocodex/*` recovery refs 到 remote，不会 push、prune 或 fast-forward 其他开发者 branch。被拒绝的 sync 不会修改远端 ref。失败或超时只打印 warning，不应改变 `sync` 的退出状态。

没有手动恢复命令。`sync` 是 owner session 的恢复入口；`status` 和 `log`
只提供只读诊断。`delete <name>` 是 operator 用来清理已废弃 session 的维护命令，
不是 active sync task 的恢复命令。

daemon 不会自动把 dirty session 入队。dirty work 会留在本地，直到 owner 显式运行 `sync`。

direct publish 只允许用于该 session 记录的 `last_seen_main` 仍然等于当前本地 `main` 的情况。如果 worktree 有未提交修改，Cocodex 会用该 session 配置的 Git identity 创建 snapshot commit，然后 fast-forward 本地 `main`。如果另一个 session 已经先发布，条件会变为 false。后面的 session 会拿到 integration lock，snapshot 自己的工作，并先尝试把最新 `main` 普通 Git merge 到这个 snapshot 上。Git merge candidate 只有在 worktree clean、candidate 同时包含最新 `main` 和 session snapshot、且 candidate diff 通过 `git diff --check` 时才会被接受。如果这个轻量路径失败，Cocodex 会把 worktree 重置到最新 `main` 并进入正常语义融合 task。所有 publish 路径都会预检查项目仓库 main worktree；如果 main worktree dirty 或存在 unsafe Git state，本次 `sync` 会在移动 `main` 前被拒绝，session 工作仍保留在 managed worktree 或 snapshot ref 中以便重试。

任何 publish 路径都不会 fast-forward clean idle sessions。其他开发者的 worktree 只有在他们自己从 managed worktree 运行 `cocodex sync` 时才会移动。

并发 sync 请求采用 fail-fast 语义。如果另一个 session 已经持有 integration lock，`ready_to_integrate` 会返回 `integration busy`，而不是把第二个 session 放进等待队列。Cocodex 不维护持久的多人 sync queue。

`join <name>` 会从 `config.developers[name]` 解析开发者配置。Cocodex 使用其中的 `git_user_name` 和 `git_user_email`，启用 Git `extensions.worktreeConfig`，并用 `git config --worktree` 写入 `user.name`/`user.email`，这样同一个服务器账号下的不同开发者也能在各自 managed worktree 中使用不同提交身份。如果该 entry 没有 `command`，Cocodex 默认启动 `codex`；否则使用配置里的 JSON 字符串数组。CLI 不再接受 Git identity 覆盖参数；配置文件是开发者 identity 和启动命令的唯一来源。

当 `join` 在 tmux 里运行时，`_resolve_tmux_target()` 会默认把 session agent 绑定到当前 pane。这是有意自动化的：否则 daemon 可以创建 sync task，但正在运行的 Codex 只能看到打印出来的文件路径，而收不到完整 prompt。需要不同 pane 的启动脚本应显式传入 `--tmux-target`。
非交互维护脚本和测试 harness 应设置 `COCODEX_NO_TMUX=1`，避免继承到的 `TMUX_PANE` 把测试 prompt 粘贴进 operator 当前的 Codex session。

每次 `join` 启动 session command 前，Cocodex 都会调用 `prepare_join_startup_notice()`，让重启行为变成显式流程：

- active task 会重新提示 task file 和 validation file；
- 旧版本遗留的 `blocked`、`recovery_required`、`queued` 或被中断的启动状态，如果 active task 可继续，会归一化到 `fusing`；如果 task 尚未安全启动，则尽量恢复 snapshot 后回到 `clean`；
- 仅仅落后于 `main` 的 clean session 会收到 catch-up notice，但 worktree 不会被移动；
- 尚未集成的本地工作会产生“先 review，再开始新工作”的 notice。

`SessionAgent` 会在子命令启动后打印 startup notice。如果检测到或配置了 tmux target，也会把 notice 粘贴进该 pane。

## 生成的 Session 指导文件

`ensure_session_worktree()` 会在每个 managed worktree 中写入一个 `AGENTS.md`。这个文件告诉 Codex 它正在 Cocodex session 中工作，并说明正常协作是在该 worktree 中运行 `cocodex sync`。

这个生成文件不能让 session 一启动就变 dirty。Cocodex 在写入前会把 `/AGENTS.md` 加入仓库本地的 `.git/info/exclude`，因此 Git status、snapshot 和 `git add -A` 都会忽略它。如果项目本身已经有自己的 `AGENTS.md`，Cocodex 不会覆盖项目指令。

## 状态模型

每个 session 对应一个 `SessionRecord`：

- `name`：稳定的 session id，例如 `alice`。
- `branch`：managed session branch，通常是 `cocodex/<name>`。
- `worktree`：managed Git worktree 路径。
- `state`：session 生命周期状态。
- `last_seen_main`：该 session 已知同步到的 main commit。
- `active_task`：当前 integration task id。
- `blocked_reason`：保留给旧状态记录的字段；正常拒绝路径不会持久写入 blocked reason。
- `pid`、`control_socket`、`last_heartbeat`、`connected`、`agent_version`：运行时 metadata。

SQLite 还保存：

- 全局 integration lock；
- `last_observed_main` 等 key/value metadata；
- 用于 status/debug 的 event log。

lock 和 `active_task` 必须保持一致。`claim_integration_task()` 会在同一个 SQLite 事务中记录 session task id 和 lock owner，然后才开始 snapshot 工作。如果 task 启动不能安全完成，Cocodex 会在可能时恢复 snapshot，清理 active task，释放 lock，并拒绝当前 `sync`。

## Session 状态

重要状态如下：

- `clean`：相对已知 main 没有待集成改动。
- `dirty`：存在需要集成的本地改动或 commit。该状态由显式 sync 路径进入，不由 daemon 自动扫描入队。
- `snapshot`：daemon 正在准备 snapshot。
- `frozen`：session 已确认 freeze。
- `fusing`：拥有任务的 Codex 正在最新 `main` 上融合 snapshot。
- `verifying`：Cocodex 正在验证 candidate。
- `publishing`：Cocodex 正在移动 `main` 并可选推送 remote。
- `queued`、`blocked`、`recovery_required`：旧状态，会在 daemon 启动、join 或 sync 时归一化；它们不是正常目标状态。
- `abandoned`：旧版本手动恢复留下的状态；不是正常目标状态。

## 控制协议

Session 发给 daemon：

- `register`：注册 session agent 和 runtime metadata。
- `heartbeat`：维持连接状态并报告 agent 版本。
- `shutdown`：标记 session 断开。
- `ready_to_integrate`：`cocodex sync` 使用的请求；它会在返回前完成 direct publish、clean Git merge publish 或 semantic task 启动。
- `fusion_done`：`cocodex sync` 使用的内部 candidate-ready 信号。

Daemon 发给 session：

- `freeze`：要求 agent 停止进入新的开发动作窗口。
- `start_fusion`：让 agent 写出 prompt file，并把 prompt 注入 session pane。

`src/cocodex/protocol.py` 负责消息结构校验，`src/cocodex/transport.py` 负责 JSONL socket transport。

`register` 和 `heartbeat` 会带上 `agent_version`。daemon 会和自己的 package version 比较。过期 agent 会在 sync/register 边界被拒绝，但不会把 session 写入持久 blocked 状态。`status` 会显示 `version_mismatch=true`，方便 operator 在升级 Cocodex 后重启旧的 `cocodex join` agent。

## Integration 流程

daemon loop 依次执行：

1. heartbeat timeout 检测；
2. external `main` movement 检测；
3. event log 输出。

`ready_to_integrate` 会在来自 `cocodex sync` 的同一次请求内完成 integration 启动。它会同时认领 lock 和 active task，发送 `freeze`，准备 snapshot，把 snapshot/base ref 保存到 `refs/cocodex/`，尝试 clean Git merge fast path，然后在返回前完成发布或启动 semantic task。第二个 session 会收到 `integration busy`，之后需要重试。

semantic task 只有在 `start_fusion` 返回 ack 且 prompt delivery 成功后才算启动成功。这里的 delivery 指 prompt file 已写出，并且 prompt 已粘贴到配置的 tmux pane；Cocodex 不会自动发送 Enter。如果 prompt delivery 失败，Cocodex 会把 snapshot commit 恢复到 session worktree，释放 lock，清理 active task，并拒绝这次 `sync`。

task file 由 `src/cocodex/tasks.py` 创建，包含 snapshot commit、latest main、last seen main、diff summary、中断当前开发请求时的处理要求、语义并集要求、矛盾处理规则、validation report 要求，以及提交 candidate 后在同一个 worktree 中再次运行 `cocodex sync` 的指令。

## 发布流程

active-task `sync` 会触发 `publish_candidate()`。

发布前会检查：

- session 和 task id 匹配；
- integration lock 属于同一个 session/task；
- task file、snapshot ref 和 base ref 都仍然存在；
- worktree 没有未完成的危险 Git 操作；
- candidate 等于 session `HEAD`；
- candidate 不是 task base commit，除非 Codex 创建了显式 no-op commit；
- validation 前 worktree 干净；
- task validation report 存在且有有效内容；
- validation 没有伴随 `HEAD` 改变或弄脏 worktree；
- 项目仓库 main worktree clean，且没有 unsafe Git state；
- 本地 `main` 可以 fast-forward 到 candidate。

本地 publish 后，Cocodex 会记录 `last_observed_main`、将发布的 session 标记为 clean，并释放 lock。其他 session worktree 不会被移动或通知。如果配置了 remote，CLI 随后会对 `main_branch`、发布 session branch 和 `refs/cocodex/*` recovery refs 尝试 best-effort scoped remote sync。remote sync 失败不再阻塞本地发布：Cocodex 会打印 warning，并在后续成功的 `sync` 中重试。

发布成功后，Cocodex 会：

1. 将 session 标记为 clean；
2. 更新 `last_seen_main`；
3. 释放 lock。

## Git Merge Fast Path

对于 `last_seen_main` 已过期的 dirty session，`ready_to_integrate` 会先获取 integration lock 并 freeze session agent。`prepare_locked_sync()` 随后 snapshot session work，并调用 `publish_with_git_merge_if_clean()`。这条路径会在 session worktree 内运行 `git merge --no-ff`，再执行上面描述的轻量结构检查。clean merge 成功时，Cocodex 直接发布到本地 `main`，记录 `published with git merge`，不会创建 task file，也不会打扰 Codex session。merge conflict、unsafe Git state、merge 后 worktree dirty、缺失 ancestry 或 `git diff --check` 失败，都会进入语义 fallback：Cocodex 会 abort 或 reset 掉 merge，保留 snapshot ref，然后创建正常 Cocodex task。

## 拒绝与启动恢复语义

Cocodex 的原则是拒绝不安全动作，而不是写入持久 blocked 状态。

Heartbeat timeout：

- stale connected session 会被标记为 disconnected；
- 如果 stale session 拥有 lock，lock 和 `fusing` active task 会保留；其他 session 会收到 `integration busy`，直到 owner 重新 join 并从自己的 worktree 运行 `cocodex sync`。

Startup recovery：

- 有 task file 且 lock 匹配的 active task 会归一化到 `fusing`；
- 如果 lock owner 与 session 匹配但 task id 不一致，Cocodex 会先为当前
  worktree 创建 backup ref，再以 lock task 为准重写 session `active_task`；
- 没有可用 task file 的不完整 task 启动状态，会先在 `refs/cocodex/backups/...` 下创建 backup ref，再在可能时恢复 snapshot ref，清理 active task，并释放 lock；
- 旧版本遗留的 `queued`、无 active task 的 `blocked`、无 active task 的 `recovery_required` 会回到 `clean`；实际是否有工作仍由 worktree head 和 dirty 状态判断。
- 指向未知 session 的旧 queue row 会被清理，不会调度任何工作。

Unknown baseline recovery：

- 如果 `last_seen_main` 缺失，但 Git ancestry 证明 session 只是领先当前 `main`，Cocodex 会采用当前 `main` 作为 baseline，并允许正常发布；
- 如果 session 只是落后当前 `main`，Cocodex 会采用 session head 作为 baseline，以便普通 catch-up fast-forward；
- 真正 divergent 的 unknown-baseline session 会被拒绝，等待 operator 检查。

External main detection：

- 对比本地 `main` 和 `last_observed_main`；
- 记录事件并更新 observed value；Cocodex 假设正常 main 移动来自 Cocodex 自己，publish 时的 fast-forward 检查仍会拒绝 stale candidate。

拒绝输出：

- CLI 失败提示集中在 `src/cocodex/failures.py`。
- `format_failure_handling()` 会打印 `Cocodex sync refused` 区块，说明下一步安全动作：busy lock 后重试、同 session 完成 task、版本不一致重启、启动 daemon、或 main guard 修正。
- `format_status()` 会调用 `next_step_for_session()`，并在 active task 旁显示 task file、validation file、snapshot ref、base ref 和明确下一步。
- daemon 通过 transport 返回的 `error` 仍保持简短；CLI 在非零退出前追加本地失败处理指引。

维护者新增拒绝状态时，应保持这个规则：每个用户可见的 refusal path 都必须在输出或文档中回答三个问题：

1. 这是同一个 session 自己处理，还是 operator 处理？
2. 当前 worktree 应该保持不动、修复、重新 join，还是稍后重试？
3. 下一条应该运行的 Cocodex 命令是什么？

Task recovery：

- active task 拒绝会保留 task id、保留 lock，并让 session 保持 `fusing`；拥有该任务的 Codex 修复 task 问题后再次运行 `cocodex sync`；
- disconnected active task 属于同 owner 可恢复状态：`cocodex join <name>` 会重新提示 task，同一个 session 在提交 candidate 并写好 validation 后再次运行 `cocodex sync` 可以发布；
- `cocodex status` 会显示 active session 的 task file、validation file、snapshot/base refs、lock owner 和下一步提示；
- 旧版本遗留的 taskless 状态会由 `sync`、`join` 或 daemon startup 归一化；不能安全处理的状态会被拒绝并说明 owner session 应该执行的动作，而不是要求手动恢复命令。

## Session 删除

`cocodex delete <name>` 的实现位于 `src/cocodex/delete.py`。它用于把已废弃
session 从本地 Cocodex state 和 managed Git 资源中移除，同时保留可恢复面。

以下情况会拒绝执行：

- session 仍 connected，或记录的进程 pid 看起来仍然存活；
- session 持有 integration lock；
- session 有 active task；
- managed worktree 不在预期的 `cocodex/<name>` branch；
- worktree 中存在未完成的 Git 操作；
- session branch 被其他 worktree checkout；
- worktree 中有除 Cocodex 生成的 `AGENTS.md` 之外的 ignored files。

成功时，命令会创建 `.cocodex/deleted/<timestamp>-<name>.json`，把 session
branch head 保存到 `refs/cocodex/deleted/<timestamp>/<name>/head`。如果
worktree 有 tracked 或 untracked 改动，还会用
`git stash push --include-untracked` 生成 dirty commit，并保存到
`refs/cocodex/deleted/<timestamp>/<name>/dirty`。只有在这些 ref 和 manifest
写好之后，才会移除 worktree、删除本地 `cocodex/<name>` branch、删除
`sessions` 和 `queue` 行，并记录 `session_deleted` event。

`.cocodex/config.json` 里的 developer entry 不会被自动删除。配置表示谁可以
join；session 删除只移除当前本地 session 实例。如果设置了 `config.remote`，
delete 会 best-effort push deleted-session refs，并删除远端 session branch。
远端清理失败只作为本地清理之后的 warning，这和 `sync` 的远端失败策略一致。

## 维护说明

公开发布树包含 `tests/` 下的 Cocodex release scenario tests。这些测试会随 source distribution 一起发布，便于用户和维护者复现 PyPI 发布前使用的端到端检查。测试运行时创建的临时仓库放在 `COCODEX_TEST_ROOT` 指定的位置；如果没有设置该环境变量，则默认使用 `~/coconut-tests`。在当前开发环境中，这个默认路径是 `/root/coconut-tests`。

常用本地检查命令：

```bash
python tests/run_release_scenarios.py
python -m pytest -q
PYTHONPATH=src python3 -m cocodex --help
git diff --check HEAD
```

如果排查 `sync` 为什么没有更新远端仓库，先看 `cocodex status`。如果显示
`remote: none`，说明 `config.remote` 是 `null`，`try_force_push_session_refs()`
会按设计直接返回，不会 push；即使底层 Git 仓库有 `origin` remote 也是如此。
如果 semantic task 已经启动，还要检查远端是否存在
`refs/cocodex/snapshots/<task>`；这些 ref 是恢复面的一部分。

## PyPI 发布

Cocodex 使用 `setup.cfg` 作为唯一 packaging metadata 来源。`pyproject.toml` 只声明 build backend，`setup.py` 只是调用 `setup()` 的兼容 shim；不要把版本号或包 metadata 写到 `pyproject.toml` 或 `setup.py` 中。

发布由 `.github/workflows/release.yml` 处理。workflow 在推送 `v*.*.*` tag 时运行：构建 wheel 和 sdist，用 `twine` 检查，确认 tag 版本与 `metadata.version` 一致，然后通过 PyPI Trusted Publishing 发布到 PyPI。正常发布路径不需要在 GitHub Secrets 中保存 PyPI API token。

第一次发布前的一次性设置：

1. 在 GitHub 仓库的 `Settings -> Environments` 中创建 `pypi` environment。
2. 在 `pypi` environment 的 deployment protection rules 中添加 `Required reviewers`。如果当前只有一个维护者，不要开启 `Prevent self-review`，否则 publish job 可能一直等待无人能 approve。
3. 在 PyPI 中为项目 `cocodex` 配置 project 或 pending publisher：owner 填 `ivowang`，repository 填 `cocodex`，workflow 填 `release.yml`，environment 填 `pypi`。

发布步骤：

```bash
# 先修改 setup.cfg 的 metadata.version
python -m pip install --upgrade build twine
rm -rf dist build *.egg-info src/*.egg-info
python tests/run_release_scenarios.py
python -m build
python -m twine check --strict dist/*
git add setup.cfg
git commit -m "Release X.Y.Z"
git tag vX.Y.Z
git push origin main
git push origin vX.Y.Z
```

tag push 后，打开 GitHub Actions 里的对应 workflow run。build job 不需要审批；publish job 会停在 `pypi` environment 等待 approval。通过 `Review deployments` approve 后，GitHub Actions 才会把已经构建好的 artifacts 发布到 PyPI。PyPI 文件是不可变的，一旦某个版本上传成功，之后不要复用这个版本号。

发布前确认公开树只包含：

- `src/cocodex/`；
- `.github/workflows/release.yml`；
- `MANIFEST.in`；
- `pyproject.toml`；
- `setup.cfg`；
- `setup.py`；
- `README.md`；
- `docs/README_ZH.md`；
- `docs/DEV.md`；
- `docs/DEV_ZH.md`；
- `tests/`；
- `.gitignore` 等必要项目 metadata。

不要发布 `.cocodex/`、`.pytest_cache/`、`__pycache__/`、内部计划文档或临时 scratch 目录。
