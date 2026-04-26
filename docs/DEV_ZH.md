# Coconut 开发者文档

本文面向 Coconut 项目的维护者，说明当前实现模型和关键状态机。用户使用流程请阅读根目录的 [README.md](../README.md) 或 [中文 README](README_ZH.md)。

## 架构

Coconut 是围绕 Git 和 Codex 构建的单机协作编排层，主要由四部分组成：

- CLI：`src/coconut/cli.py`。
- 持久状态：`src/coconut/state.py`，使用 `.coconut/state.sqlite` 中的 SQLite。
- Daemon 编排：`src/coconut/daemon.py`。
- Session 侧协作 agent：`src/coconut/agent.py`。

daemon 和 session agent 通过 Unix domain socket 传输 JSONL 消息。Git 操作通过 `src/coconut/git.py` 中的 helper 调用 Git CLI 完成。

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
- `dirty`：存在需要集成的本地改动或 commit。
- `queued`：等待 daemon 开始 integration。
- `snapshot`：daemon 正在准备 snapshot。
- `frozen`：session 已确认 freeze。
- `fusing`：拥有任务的 Codex 正在最新 `main` 上融合 snapshot。
- `verifying`：Coconut 正在验证 candidate。
- `publishing`：Coconut 正在移动 `main` 并可选推送 remote。
- `blocked`：语义冲突或验证失败，需要人工/Codex 处理。
- `recovery_required`：继续自动处理可能丢失改动或错误发布，因此需要显式恢复。
- `abandoned`：session task 被手动放弃。

## 控制协议

Session 发给 daemon：

- `register`：注册 session agent 和 runtime metadata。
- `heartbeat`：维持连接状态。
- `shutdown`：标记 session 断开。
- `ready_to_integrate`：请求入队。公开 CLI 中对应 `coconut ready <session>`。daemon 只会在该 session 确实有待集成改动时入队。
- `fusion_done`：报告当前 candidate 可以验证和发布。
- `fusion_blocked`：报告 Codex 无法安全完成 integration。

Daemon 发给 session：

- `freeze`：要求 agent 停止进入新的开发动作窗口。
- `start_fusion`：让 agent 打印生成的 task file 路径。
- `main_updated`：通知 session 本地 `main` 已更新。

`src/coconut/protocol.py` 负责消息结构校验，`src/coconut/transport.py` 负责 JSONL socket transport。

## 队列和 Integration 流程

daemon loop 依次执行：

1. heartbeat timeout 检测；
2. external `main` movement 检测；
3. dirty session 扫描；
4. 一次 queue processing attempt。

`process_queue_once()` 只有在 integration lock 空闲时才会启动任务。它会同时认领 lock 和 active task，发送 `freeze`，准备 snapshot，将 session worktree 重置到最新 `main`，写出 task file，然后发送 `start_fusion`。

task file 由 `src/coconut/tasks.py` 创建，包含 snapshot commit、latest main、last seen main、diff summary、验证命令和完成指令。
生成的完成指令会写明具体 CLI 命令：`coconut done <session>` 和 `coconut block <session> "<reason>"`。

## 发布流程

`fusion_done` 触发 `publish_candidate()`。

发布前会检查：

- session 和 task id 匹配；
- integration lock 属于同一个 session/task；
- session 状态允许发布或重试 recovery publish；
- worktree 没有未完成的危险 Git 操作；
- candidate 等于 session `HEAD`；
- 验证前 worktree 干净；
- verification 通过；
- verification 没有改变 `HEAD` 或弄脏 worktree；
- 本地 `main` 可以 fast-forward 到 candidate。

本地 publish 后，Coconut 会按配置可选 push remote。如果本地 `main` 已经前进但 remote push 失败，Coconut 会保守处理：session 进入 `recovery_required`，锁保持占用，同一个 task 可以在远端问题修复后重试。

发布成功后，Coconut 会：

1. 将 session 标记为 clean；
2. 更新 `last_seen_main`；
3. 释放 lock；
4. 记录 `last_observed_main`；
5. fast-forward clean idle sessions；
6. 广播 `main_updated`。

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

手动恢复命令：

- `resume` 只会在 session 不再拥有 integration lock 时重新入队 blocked/recovery session。
- `abandon` 会标记 session abandoned、移出队列，并在匹配或孤儿 owner lock 存在时清除它。
- `done` 可以在同一个 task 仍 active 时重试 locked recovery publish。

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
- `README.md`；
- `docs/README_ZH.md`；
- `docs/DEV.md`；
- `docs/DEV_ZH.md`；
- `.gitignore` 等必要项目 metadata。

不要发布 `.coconut/`、`.pytest_cache/`、`__pycache__/`、内部计划文档或实现测试套件，除非发布策略发生变化。
