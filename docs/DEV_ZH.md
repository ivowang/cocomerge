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

`SessionAgent` 可以把 sync prompt 粘贴到 tmux 中。`join` 会在环境里存在 `TMUX_PANE` 时默认使用当前 pane，这符合“开发者从自己的 tmux pane 中通过 Cocodex 启动 Codex”的产品约束。高级启动器可以用 `--tmux-target` 覆盖检测到的目标。收到 `start_fusion` 后，agent 总会在 task file 旁边写出 prompt file 并打印二者路径；如果有可用目标 pane，也会额外通过 `tmux load-buffer`、`paste-buffer` 和 `send-keys Enter` 注入 prompt。

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
- 没有 active task、有本地更改且 `main != last_seen_main`：只有在没有其他 session 持有 lock 或等待 task 启动时，才通过 `ready_to_integrate` 请求语义融合任务；
- active task 处于 `fusing` 或可重试 `blocked`：通过 `fusion_done` 报告当前 session `HEAD` 是 candidate，让 daemon 校验 validation report 并发布；
- 可重试 remote publish recovery：再次通过 `fusion_done` 重试 publish 路径。

执行协议动作前，以及本地 catch-up 或 publish 成功后，CLI 会在配置了 `config.remote` 时尝试 best-effort remote sync。该操作只会 force-push 本地 `main_branch` 和当前 session branch 到 remote，不会 push、prune 或 fast-forward 其他开发者 branch。失败或超时只打印 warning，不应改变 `sync` 的退出状态。

`resume` 和 `abandon` 是 operator 恢复命令。它们不会出现在顶层 help 中，也不属于普通开发者工作流。

daemon 不会自动把 dirty session 入队。dirty work 会留在本地，直到 owner 显式运行 `sync`。

direct publish 只允许用于该 session 记录的 `last_seen_main` 仍然等于当前本地 `main` 的情况。如果 worktree 有未提交修改，Cocodex 会用该 session 配置的 Git identity 创建 snapshot commit，然后 fast-forward 本地 `main`。如果另一个 session 已经先发布，条件会变为 false，后面的 session 会进入正常语义融合路径。如果 Cocodex 已经拿到 lock 后 direct publish 失败，例如项目仓库的 main worktree 中有 Git 拒绝覆盖的本地文件，这个 session 会进入无 active task 的 `blocked`，并释放 lock。operator 修复 blocker 后运行 `cocodex resume <name>`，从该 session 已提交的 HEAD 重试。

任何 publish 路径都不会 fast-forward clean idle sessions。其他开发者的 worktree 只有在他们自己从 managed worktree 运行 `cocodex sync` 时才会移动。

并发 sync 请求采用 fail-fast 语义。如果另一个 session 已经持有 integration lock，或已有请求正在等待 daemon 启动 task，`ready_to_integrate` 会返回 `integration busy`，而不是把第二个 session 放进等待队列。FIFO queue 仍作为 sync 请求到 daemon loop 之间的内部交接结构存在，但不再是多人等待队列。

`join <name>` 会从 `config.developers[name]` 解析开发者配置。Cocodex 使用其中的 `git_user_name` 和 `git_user_email`，启用 Git `extensions.worktreeConfig`，并用 `git config --worktree` 写入 `user.name`/`user.email`，这样同一个服务器账号下的不同开发者也能在各自 managed worktree 中使用不同提交身份。如果该 entry 没有 `command`，Cocodex 默认启动 `codex`；否则使用配置里的 JSON 字符串数组。CLI 不再接受 Git identity 覆盖参数；配置文件是开发者 identity 和启动命令的唯一来源。

当 `join` 在 tmux 里运行时，`_resolve_tmux_target()` 会默认把 session agent 绑定到当前 pane。这是有意自动化的：否则 daemon 可以创建 sync task，但正在运行的 Codex 只能看到打印出来的文件路径，而收不到完整 prompt。需要不同 pane 的启动脚本应显式传入 `--tmux-target`。
非交互维护脚本和测试 harness 应设置 `COCODEX_NO_TMUX=1`，避免继承到的 `TMUX_PANE` 把测试 prompt 粘贴进 operator 当前的 Codex session。

每次 `join` 启动 session command 前，Cocodex 都会调用 `prepare_join_startup_notice()`，让重启行为变成显式流程：

- active task 会重新提示 task file 和 validation file；
- 如果 `recovery_required` 的 active task 有 task file，且 integration lock 仍属于同一个 session/task，会自动回到 `fusing`；
- queued sync request 会产生等待 task 的 notice；
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
- `blocked_reason`：阻塞或恢复原因。
- `pid`、`control_socket`、`last_heartbeat`、`connected`、`agent_version`：运行时 metadata。

SQLite 还保存：

- 等待 integration 的 FIFO queue；
- 全局 integration lock；
- `last_observed_main` 等 key/value metadata；
- 用于 status/debug 的 event log。

lock 和 `active_task` 必须保持一致。队列处理通过 `claim_integration_task()` 在同一个 SQLite 事务中记录 session task id 和 lock owner，避免 daemon 崩溃时留下孤儿锁。正常运行中 queue 是 single-flight 的，因为已有 session 正在 sync 时新的 sync 会被拒绝。

## Session 状态

重要状态如下：

- `clean`：相对已知 main 没有待集成改动。
- `dirty`：存在需要集成的本地改动或 commit。该状态由显式 sync 路径进入，不由 daemon 自动扫描入队。
- `queued`：等待 daemon 开始 integration。
- `snapshot`：daemon 正在准备 snapshot。
- `frozen`：session 已确认 freeze。
- `fusing`：拥有任务的 Codex 正在最新 `main` 上融合 snapshot。
- `verifying`：Cocodex 正在验证 candidate。
- `publishing`：Cocodex 正在移动 `main` 并可选推送 remote。
- `blocked`：可能是 active sync task 需要同一个 session 修复后再次运行 `sync`，也可能是需要 operator 修复后 resume 的外部 blocker。
- `recovery_required`：继续自动处理可能丢失改动或错误发布，因此需要显式恢复。
- `abandoned`：session task 被手动放弃。

## 控制协议

Session 发给 daemon：

- `register`：注册 session agent 和 runtime metadata。
- `heartbeat`：维持连接状态并报告 agent 版本。
- `shutdown`：标记 session 断开。
- `ready_to_integrate`：`cocodex sync` 使用的内部入队请求。
- `fusion_done`：`cocodex sync` 使用的内部 candidate-ready 信号。

Daemon 发给 session：

- `freeze`：要求 agent 停止进入新的开发动作窗口。
- `start_fusion`：让 agent 打印生成的 task file 路径。

`src/cocodex/protocol.py` 负责消息结构校验，`src/cocodex/transport.py` 负责 JSONL socket transport。

`register` 和 `heartbeat` 会带上 `agent_version`。daemon 会和自己的 package version 比较。过期的 clean session 会进入 `blocked`；带 active task 的过期 session 会进入 `recovery_required`。`status` 会显示 daemon 和 agent 版本，方便 operator 在升级 Cocodex 后重启旧的 `cocodex join` agent。

## 队列和 Integration 流程

daemon loop 依次执行：

1. heartbeat timeout 检测；
2. external `main` movement 检测；
3. 一次 queue processing attempt。

`process_queue_once()` 只有在 integration lock 空闲时才会启动任务。它会同时认领 lock 和 active task，发送 `freeze`，准备 snapshot，把 snapshot/base ref 保存到 `refs/cocodex/`，将 session worktree 重置到最新 `main`，写出 task file，然后发送 `start_fusion`。第二个 session 不能通过 `sync` 排在它后面；该命令会收到 `integration busy`，之后需要重试。

task file 由 `src/cocodex/tasks.py` 创建，包含 snapshot commit、latest main、last seen main、diff summary、中断当前开发请求时的处理要求、validation report 要求，以及提交 candidate 后在同一个 worktree 中再次运行 `cocodex sync` 的指令。

## 发布流程

active-task `sync` 会触发 `publish_candidate()`。

发布前会检查：

- session 和 task id 匹配；
- integration lock 属于同一个 session/task；
- recovery retry 只限 remote-push recovery 或 startup-publishing recovery；
- worktree 没有未完成的危险 Git 操作；
- candidate 等于 session `HEAD`；
- candidate 不是 task base commit，除非 Codex 创建了显式 no-op commit；
- validation 前 worktree 干净；
- task validation report 存在且有有效内容；
- validation 没有伴随 `HEAD` 改变或弄脏 worktree；
- 本地 `main` 可以 fast-forward 到 candidate。

本地 publish 后，Cocodex 会记录 `last_observed_main`、将发布的 session 标记为 clean，并释放 lock。其他 session worktree 不会被移动或通知。如果配置了 remote，随后会对 `main_branch` 和发布 session branch 尝试 best-effort scoped remote sync。remote sync 失败不再阻塞本地发布：Cocodex 会记录 `remote_sync_failed` event，并在后续 `sync` 中重试。

发布成功后，Cocodex 会：

1. 将 session 标记为 clean；
2. 更新 `last_seen_main`；
3. 释放 lock。

## 恢复语义

Cocodex 的恢复原则是宁可停止，也不猜测。

Heartbeat timeout：

- stale connected session 会被标记为 disconnected；
- 如果 stale session 拥有 lock，它会进入 `recovery_required`，并保留 lock 以便显式恢复。

Startup recovery：

- 不完整 integration 状态会进入 `recovery_required`；
- 不一致的 owner lock 会被认领到 owner session 的 `active_task`，这样可以显式 abandon 或检查，而不是留下无法处理的孤儿锁。

External main detection：

- 对比本地 `main` 和 `last_observed_main`；
- 如果 `main` 被 Cocodex 以外的过程移动，dirty/queued/active integration session 会进入 `recovery_required`；
- 如果本地 `main` 等于某个持锁 session 的 candidate，且处于 Cocodex 自己的 pending publish recovery，不会被误判为 external movement。

手动恢复命令只面向 operator。

失败输出：

- CLI 失败提示集中在 `src/cocodex/failures.py`。
- `format_failure_handling()` 会按常见失败原因分类，并打印下一步安全动作：busy lock 后重试、同 session 修复 task、operator resume、版本不一致重启、启动 daemon、或 main guard 修正。
- `format_task_status()` 会调用 `next_step_for_session()`，因此 `cocodex task <name>` 会在 task refs 旁边显示一个明确的下一步。
- daemon 通过 transport 返回的 `error` 仍保持简短；CLI 在非零退出前追加本地失败处理指引。

维护者新增失败状态时，应保持这个规则：每个用户可见的 fail path 都必须在输出或文档中回答三个问题：

1. 这是同一个 session 自己处理，还是 operator 处理？
2. 当前 worktree 应该保持不动、修复、resume，还是 abandon？
3. 下一条应该运行的 Cocodex 命令是什么？

Blocked recovery：

- 带 active task 的 `blocked` 通常保留 task id，并且 lock 仍属于该 task；拥有该任务的 Codex 修复 task 问题后再次运行 `cocodex sync`；
- `cocodex task <name>` 会显示单个 session 的 task file、validation file、snapshot/base refs、lock owner 和恢复提示；
- `cocodex resume <name>` 会在 lock 下恢复 active task，并在 session agent 已连接时重新发送 task prompt。对于无 active task 的 `blocked`，operator 修复 `blocked_reason` 后，它会把该 session 放入重试；
- `abandon` 会先在 `refs/cocodex/backups/...` 下创建 backup ref，再清理 Cocodex 对某个 session 的 task/queue/lock 记录。它不会 revert 任何 worktree 中的文件或 commit。

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
