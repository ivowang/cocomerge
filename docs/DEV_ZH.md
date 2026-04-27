# Coconut 开发者文档

本文面向 Coconut 项目的维护者，说明当前实现模型和关键状态机。用户使用流程请阅读根目录的 [README.md](../README.md) 或 [中文 README](README_ZH.md)。

## 架构

Coconut 是围绕 Git 和 Codex 构建的单机协作编排层，主要由以下部分组成：

- CLI：`src/coconut/cli.py`。
- 持久状态：`src/coconut/state.py`，使用 `.coconut/state.sqlite` 中的 SQLite。
- Daemon 编排：`src/coconut/daemon.py`。
- Session 侧协作 agent：`src/coconut/agent.py`。
- Session worktree 初始化：`src/coconut/session.py`，包括为 Codex 生成 Coconut 指导文件和配置 per-worktree Git identity。

daemon 和 session agent 通过 Unix domain socket 传输 JSONL 消息。Git 操作通过 `src/coconut/git.py` 中的 helper 调用 Git CLI 完成。

`SessionAgent` 可以把 sync prompt 粘贴到 tmux 中，但只有在 `join` 显式收到 `--tmux-target` 时才会这么做。Coconut 刻意不自动识别 `TMUX_PANE`，因为测试、包装脚本和嵌套 shell 可能继承到错误 Codex 的环境变量。收到 `start_fusion` 后，agent 总会在 task file 旁边写出 prompt file 并打印二者路径；如果配置了目标 pane，它才会额外通过 `tmux load-buffer`、`paste-buffer` 和 `send-keys Enter` 注入 prompt。

## 产品命令模型

普通开发者命令是：

```bash
coconut sync
```

这个命令在 managed worktree 中执行。CLI 会用当前 Git worktree root 匹配已注册的 `SessionRecord.worktree` 来推断 session。显式 session 参数仍保留给 main repository 中的 operator/internal 用法。

内部实现中，`sync` 会根据状态映射到不同协议动作：

- 没有 active task：通过 `ready_to_integrate` 请求入队；
- active task 处于 `fusing` 或可重试 `blocked`：通过 `fusion_done` 报告当前 session `HEAD` 是 candidate，让 daemon 验证和发布；
- 可重试 remote publish recovery：再次通过 `fusion_done` 重试 publish 路径。

执行协议动作前，以及本地 catch-up 或 publish 成功后，CLI 会在配置了 `config.remote` 时尝试 best-effort remote sync。该操作会 force-push/prune 本地 `refs/heads/*` 到 remote；如果存在 Coconut 内部 `refs/coconut/*` namespace，也会一并推送。失败或超时只打印 warning，不应改变 `sync` 的退出状态。

`done`、`block`、`resume`、`abandon` 等 legacy/internal 命令只面向 operator 或兼容场景，不属于普通开发者工作流。

daemon 不会自动把 dirty session 入队。dirty work 会留在本地，直到 owner 显式运行 `sync`。

`join` 接受 `--git-user-name` 和 `--git-user-email`。传入后，Coconut 会启用 Git `extensions.worktreeConfig`，并用 `git config --worktree` 写入 `user.name`/`user.email`，这样同一个服务器账号下的不同开发者也能在各自 managed worktree 中使用不同提交身份。如果没有传入，worktree 必须已经能读取到有效 Git identity。

## 生成的 Session 指导文件

`ensure_session_worktree()` 会在每个 managed worktree 中写入一个 `AGENTS.md`。这个文件告诉 Codex 它正在 Coconut session 中工作，并说明正常协作是在该 worktree 中运行 `coconut sync`。

这个生成文件不能让 session 一启动就变 dirty。Coconut 在写入前会把 `/AGENTS.md` 加入仓库本地的 `.git/info/exclude`，因此 Git status、snapshot 和 `git add -A` 都会忽略它。如果项目本身已经有自己的 `AGENTS.md`，Coconut 不会覆盖项目指令。

## 状态模型

每个 session 对应一个 `SessionRecord`：

- `name`：稳定的 session id，例如 `alice`。
- `branch`：managed session branch，通常是 `coconut/<name>`。
- `worktree`：managed Git worktree 路径。
- `state`：session 生命周期状态。
- `last_seen_main`：该 session 已知同步到的 main commit。
- `active_task`：当前 integration task id。
- `blocked_reason`：阻塞或恢复原因。
- `pid`、`control_socket`、`last_heartbeat`、`connected`：运行时 metadata。

SQLite 还保存：

- 等待 integration 的 FIFO queue；
- 全局 integration lock；
- `last_observed_main` 等 key/value metadata；
- 用于 status/debug 的 event log。

lock 和 `active_task` 必须保持一致。队列处理通过 `claim_integration_task()` 在同一个 SQLite 事务中记录 session task id 和 lock owner，避免 daemon 崩溃时留下孤儿锁。

## Session 状态

重要状态如下：

- `clean`：相对已知 main 没有待集成改动。
- `dirty`：存在需要集成的本地改动或 commit。当前不再由 daemon 自动扫描入队，只保留给显式 sync 路径或历史状态。
- `queued`：等待 daemon 开始 integration。
- `snapshot`：daemon 正在准备 snapshot。
- `frozen`：session 已确认 freeze。
- `fusing`：拥有任务的 Codex 正在最新 `main` 上融合 snapshot。
- `verifying`：Coconut 正在验证 candidate。
- `publishing`：Coconut 正在移动 `main` 并可选推送 remote。
- `blocked`：active sync task 需要同一个 session 修复后再次运行 `sync`，或由 operator 检查。
- `recovery_required`：继续自动处理可能丢失改动或错误发布，因此需要显式恢复。
- `abandoned`：session task 被手动放弃。

## 控制协议

Session 发给 daemon：

- `register`：注册 session agent 和 runtime metadata。
- `heartbeat`：维持连接状态。
- `shutdown`：标记 session 断开。
- `ready_to_integrate`：`coconut sync` 使用的内部入队请求。
- `fusion_done`：`coconut sync` 使用的内部 candidate-ready 信号。
- `fusion_blocked`：legacy/internal 阻塞信号。

Daemon 发给 session：

- `freeze`：要求 agent 停止进入新的开发动作窗口。
- `start_fusion`：让 agent 打印生成的 task file 路径。
- `main_updated`：通知 session 本地 `main` 已更新。

`src/coconut/protocol.py` 负责消息结构校验，`src/coconut/transport.py` 负责 JSONL socket transport。

## 队列和 Integration 流程

daemon loop 依次执行：

1. heartbeat timeout 检测；
2. external `main` movement 检测；
3. 一次 queue processing attempt。

`process_queue_once()` 只有在 integration lock 空闲时才会启动任务。它会同时认领 lock 和 active task，发送 `freeze`，准备 snapshot，把 snapshot/base ref 保存到 `refs/coconut/`，将 session worktree 重置到最新 `main`，写出 task file，然后发送 `start_fusion`。

task file 由 `src/coconut/tasks.py` 创建，包含 snapshot commit、latest main、last seen main、diff summary、验证命令，以及提交 candidate 后在同一个 worktree 中再次运行 `coconut sync` 的指令。

## 发布流程

active-task `sync` 会触发 `publish_candidate()`。

发布前会检查：

- session 和 task id 匹配；
- integration lock 属于同一个 session/task；
- recovery retry 只限历史 remote-push recovery 或 startup-publishing recovery；
- worktree 没有未完成的危险 Git 操作；
- candidate 等于 session `HEAD`；
- candidate 不是 task base commit，除非 Codex 创建了显式 no-op commit；
- 验证前 worktree 干净；
- verification 通过；
- verification 没有改变 `HEAD` 或弄脏 worktree；
- 本地 `main` 可以 fast-forward 到 candidate。

本地 publish 后，Coconut 会记录 `last_observed_main`、将 session 标记为 clean、释放 lock、fast-forward clean idle sessions，并广播 main update。如果配置了 remote，随后会尝试 best-effort server-ref sync。remote sync 失败不再阻塞本地发布：Coconut 会记录 `remote_sync_failed` event，并在后续 `sync` 中重试。

发布成功后，Coconut 会：

1. 将 session 标记为 clean；
2. 更新 `last_seen_main`；
3. 释放 lock；
4. fast-forward clean idle sessions；
5. 广播 `main_updated`。

## 恢复语义

Coconut 的恢复原则是宁可停止，也不猜测。

Heartbeat timeout：

- stale connected session 会被标记为 disconnected；
- 如果 stale session 拥有 lock，它会进入 `recovery_required`，并保留 lock 以便显式恢复。

Startup recovery：

- 不完整 integration 状态会进入 `recovery_required`；
- 不一致的 owner lock 会被认领到 owner session 的 `active_task`，这样可以显式 abandon 或检查，而不是留下无法处理的孤儿锁。

External main detection：

- 对比本地 `main` 和 `last_observed_main`；
- 如果 `main` 被 Coconut 以外的过程移动，dirty/queued/active integration session 会进入 `recovery_required`；
- 如果本地 `main` 等于某个持锁 session 的 candidate，且处于 Coconut 自己的 pending publish recovery，不会被误判为 external movement。

手动恢复命令只面向 operator。

## 维护说明

公开发布树刻意排除了内部实现测试套件和计划文档。维护者应该在开发 checkout 中完成验证，然后再生成干净的公开发布树。

当验证套件存在时，常用本地检查命令：

```bash
pytest -q
PYTHONPATH=src python3 -m coconut --help
git diff --check HEAD
```

发布前确认公开树只包含：

- `src/coconut/`；
- `pyproject.toml`；
- `setup.py`；
- `README.md`；
- `docs/README_ZH.md`；
- `docs/DEV.md`；
- `docs/DEV_ZH.md`；
- `.gitignore` 等必要项目 metadata。

不要发布 `.coconut/`、`.pytest_cache/`、`__pycache__/`、内部计划文档或实现测试套件，除非发布策略发生变化。
