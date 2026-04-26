# coconut

coconut 用于协调多个 Codex session 在同一个 Git 仓库中协同开发。它面向的场景是：多名开发者登录同一台服务器上的同一个系统用户，各自运行自己的 Codex，但所有人的改动最终都必须以串行、可恢复的方式进入同一条 `main`。

## 为什么需要 Coconut

vibe coding 让多人协作的节奏变快了，但 Git 主干仍然需要保持一致。多个 Codex 同时开发时，如果大家各自 merge/push，很容易出现互相覆盖、重复集成、遗漏改动或语义冲突。

Coconut 提供一个单机协调层：

- 每个开发者使用独立的 managed worktree；
- Coconut 自动发现有改动的 session 并排队；
- 同一时间只有一个 session 拥有 integration lock；
- 获得锁的 session 会收到一份 task file，要求它的 Codex 在最新 `main` 上重新实现或语义融合自己的功能；
- Coconut 对最终 candidate 进行验证、快进本地 `main`、可选推送远端，并通知其他 session。

## 当前协作模型

Coconut 是 cooperative orchestrator。它不替代 Codex，也不会自己理解业务语义或自动解决冲突。真正的语义融合仍然由拥有当前任务的 Codex 完成。

推荐部署方式：

- 所有开发者登录同一台服务器上的同一个用户；
- 大家共同使用同一个 Git 仓库；
- 启动一个长期运行的 `coconut daemon`；
- 所有 Codex 都必须通过 `coconut join` 启动；
- Coconut 是本地 `main` 的唯一写入者。

Coconut 使用本地 Git、SQLite 和 Unix domain socket。它不是分布式锁服务，也不负责跨机器协调。

## 安装

在仓库根目录执行：

```bash
pip install -e .
```

安装后会提供 `coconut` 命令。

## 团队使用流程

在项目仓库中初始化一次：

```bash
coconut init --main main --verify "pytest" --remote origin
```

配置的主分支必须已经存在，并且至少有一个 initial commit。如果使用
`--remote origin`，这个 remote 也必须已经存在。全新仓库可以先执行：

```bash
git switch -c main
git add .
git commit -m "initial commit"
git remote add origin <url>  # 只有需要 Coconut 推送远端时才需要
```

启动 daemon：

```bash
coconut daemon
```

每个开发者通过 Coconut 启动自己的 Codex：

```bash
coconut join --name alice -- codex
coconut join --name bob -- codex
```

`join` 会在 `.coconut/` 下创建或复用该 session 的 worktree，并在这个 worktree 中运行指定命令。

当某个 session 产生改动后，Coconut 会将它放入队列。轮到它集成时，daemon 会让这个 session freeze，创建 snapshot，把 worktree 重置到最新 `main`，然后在该 Codex session 中打印一份 integration task 文件路径。

task file 会告诉 Codex：

- 最新 `main` commit；
- 该 session 上次看到的 main；
- snapshot commit；
- 需要重新实现或语义融合的 diff；
- 验证命令；
- 如何报告完成或阻塞。

当 Codex 完成融合并提交 candidate 后：

```bash
coconut done alice
```

如果 Codex 无法安全完成融合：

```bash
coconut block alice "semantic conflict with auth refactor"
```

成功发布时，Coconut 会验证 candidate、快进本地 `main`、在配置了 remote 时推送远端、广播 `main_updated`，然后处理下一个 queued session。

## 命令速览

```bash
coconut init --main main --verify "pytest" --remote origin
coconut daemon
coconut join --name alice -- codex
coconut status
coconut log
coconut resume alice
coconut abandon alice
coconut done alice
coconut block alice "reason"
```

命令说明：

- `init`：创建 `.coconut/config.json` 并初始化 daemon state。
- `daemon`：运行队列处理器、socket server、heartbeat 检查、恢复检查、发布路径和 main 更新广播。
- `join`：创建或复用 session worktree，启动 session agent，注册到 daemon，并运行指定命令。
- `status`：查看 main、session 状态、队列、锁和连接 metadata。
- `log`：打印最近的 Coconut 状态事件。
- `resume`：当 blocked 或 recovery-required session 不再持有 integration lock 时，将它重新入队。
- `abandon`：放弃某个 session task，并在安全时清除对应锁。
- `done`：请求 daemon 验证并发布该 session 当前 candidate。
- `block`：标记当前 integration 被阻塞，并释放锁。

## 恢复策略

Coconut 的原则是宁可停下来，也不猜测：

- 持有 integration lock 的 session 断连时，会进入 `recovery_required`；
- daemon 在 integration 中途重启时，会保守恢复不完整状态；
- 本地 `main` 如果被 Coconut 以外的过程移动，queued 或 active dirty session 会进入 `recovery_required`；
- 如果本地 `main` 已经前进但 remote push 失败，Coconut 会保留锁，让同一个 task 在远端问题修复后重试发布。

处理异常时，先使用 `coconut status` 和 `coconut log` 查看状态，再选择 `resume`、`done`、`block` 或 `abandon`。

## 仓库内容

这个公开版本只包含 Coconut 运行时代码和用户/开发者文档。内部计划文档和实现测试套件不包含在发布树中。

实现细节请阅读 [docs/DEV_ZH.md](DEV_ZH.md)。
