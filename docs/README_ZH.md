# coconut

[English README](../README.md)

coconut 用于协调多个 Codex session 在同一个 Git 仓库中协同开发。它面向的场景是：多名开发者登录同一台服务器上的同一个系统用户，各自运行自己的 Codex，但所有人的改动最终都必须以串行、可恢复的方式进入同一条 `main`。

## 先读这一节

Coconut 不是一个自己理解代码并自动 merge 的后台机器人。它是 Codex 周围的协作编排层：

- daemon 负责观察 managed worktree、维护 integration lock、验证 candidate、移动本地 `main`，并在配置了 remote 时推送远端。
- 每个开发者必须通过 `coconut join` 启动自己的 Codex。
- 当某个 session 有改动时，Coconut 会给这个 session 自己的 Codex 创建 integration task。这个 Codex 负责在最新 `main` 上重新实现或语义融合自己的改动。
- 只有当这个 session 显式运行 `coconut done <session>` 后，Coconut 才会尝试把 candidate 发布为新的 `main`。

最重要的规则是：开发者和 Codex 不要直接 `git pull main`、`git merge main` 或 `git push main`。开发者只在自己的 Coconut worktree 中工作，本地 `main` 只由 Coconut 写入。

## 谁执行哪个命令

| 命令 | 通常由谁执行 | 含义 |
| --- | --- | --- |
| `coconut init ...` | repo owner 或团队 operator | 为这个项目仓库初始化 Coconut。 |
| `coconut daemon` | repo owner 或团队 operator | 启动长期运行的协调进程，同一个仓库保持一个 daemon。 |
| `coconut join --name alice -- codex` | 开发者 Alice | 在 Alice 的 managed worktree 中启动 Alice 的 Codex。 |
| `coconut ready alice` | Alice 的 Codex 或 Alice 本人 | 可选，告诉 daemon Alice 当前工作可以进入队列。 |
| `coconut done alice` | Alice 的 Codex，在 active task 完成后执行 | 请求 daemon 验证并发布 Alice 当前 candidate。 |
| `coconut block alice "reason"` | Alice 的 Codex 或 Alice 本人，在 active task 无法完成时执行 | 标记当前任务阻塞，并释放 integration lock。 |
| `coconut status` / `coconut log` | 共享账号上的任何人 | 查看状态和事件。 |
| `coconut resume alice` / `coconut abandon alice` | operator 或正在处理恢复的人 | 显式处理 blocked 或 recovery 状态。 |

`done` 和 `block` 不是 daemon 自动替你执行的内部动作。它们是由拥有当前任务的 Codex 或对应开发者发出的明确信号。运行 `done` 的含义是：“这个 session worktree 当前的 `HEAD` 就是候选的新 `main`，Coconut 可以验证并发布它。”

## 安装

在 Coconut 仓库根目录执行：

```bash
pip install -e .
```

安装后会提供 `coconut` 命令。

## 项目仓库准备

下面的命令要在团队实际开发的项目仓库里执行，不是在 Coconut 源码仓库里执行。

配置给 Coconut 的主分支必须已经存在，并且至少有一个 initial commit：

```bash
git switch -c main
git add .
git commit -m "initial commit"
```

如果希望 Coconut 在发布本地 `main` 后推送远端，初始化前先添加 remote：

```bash
git remote add origin <url>
```

然后初始化 Coconut：

```bash
coconut init --main main --verify "pytest" --remote origin
```

只有当 `origin` 已经存在时才使用 `--remote origin`。如果只需要本地协调，可以省略 `--remote`。

## 开始协作

在项目仓库中，用一个长期运行的终端启动 daemon：

```bash
coconut daemon
```

每个开发者通过 Coconut 启动自己的 Codex：

```bash
coconut join --name alice -- codex
coconut join --name bob -- codex
```

每个 `join` 会创建或复用：

- 一个名为 `coconut/<name>` 的 branch；
- 一个位于 `.coconut/worktrees/<name>` 的 worktree；
- 一个和 daemon 通信的 session agent。
- 一个位于该 worktree 根目录、被 Git 忽略的 `AGENTS.md`，除非项目本身已经有自己的 `AGENTS.md`。

Codex 进程会运行在这个 managed worktree 中。开发者像平常一样让 Codex 修改代码，但不要让 Codex 直接操作 `main`。生成的 `AGENTS.md` 会告诉 Codex 它正处在 Coconut 管理的多人协作 session 里，并提醒它使用 `coconut ready`、`coconut done` 和 `coconut block`，而不是直接 pull、merge 或 push `main`。

## 合并到主线是怎么触发的

一个 session 进入 integration queue 有两种方式：

1. 自动触发：daemon 每隔几秒扫描 managed worktree。如果某个 session 有未提交改动、staged 改动、untracked 文件，或者它的 session branch 上有超过 last seen `main` 的 commit，Coconut 会把它标记为 dirty 并入队。
2. 主动触发：拥有该 session 的 Codex 或开发者可以执行：

   ```bash
   coconut ready alice
   ```

   这个命令只表示“把 Alice 当前工作放入队列”。它不会发布 `main`。如果 Alice 没有任何改动，Coconut 会提示没有需要集成的内容。

当 integration lock 空闲时，daemon 会取出队列中的下一个 session，freeze 它，snapshot 它当前的工作，把这个 session worktree 重置到最新 `main`，然后在这个 session 的 Codex 终端里打印 task file 路径：

```text
Coconut task for alice: /path/to/repo/.coconut/tasks/<task>.md
```

这份 task file 就是 Coconut 交给 Codex 的任务说明。它包含：

- 最新 `main`；
- 该 session 上次看到的 `main`；
- snapshot commit；
- 需要重新实现或语义融合的 diff；
- verification 命令；
- 明确的完成命令。

## 拿到 task 后 Codex 应该做什么

当 Alice 的 Codex 收到 task：

1. 读取 task file。
2. 把当前 worktree 当作最新 `main`。
3. 在这个最新 `main` 上重新实现或语义融合 Alice snapshot 中的改动。
4. 运行相关检查。
5. 提交最终 candidate。
6. 确保 worktree 干净。
7. 执行：

   ```bash
   coconut done alice
   ```

之后 Coconut 会运行配置的 verification 命令，fast-forward 本地 `main`，如果配置了 `--remote origin` 就推送 `origin/main`，fast-forward 没有本地改动的 idle session，广播 `main_updated`，然后开始处理下一个队列任务。

如果 Alice 的 Codex 无法安全完成 task，应该执行：

```bash
coconut block alice "semantic conflict with auth refactor"
```

这会释放 integration lock 并记录阻塞原因。之后可以通过 `coconut status` 和 `coconut log` 查看状态。

## 我能不能在 Codex 里说“和 main 同步”

可以，但这句话必须落到 Coconut 工作流上。

推荐在 Alice 的 joined Codex 里这样说：

```text
Use Coconut to sync with main. Do not run git pull, git merge main, or git push
main directly. If we have local work, run coconut ready alice, wait for the
Coconut task, apply the task on latest main, commit the final candidate, and
then run coconut done alice.
```

如果 Alice 是 clean session，通常不需要做任何事。Coconut 发布新的 `main` 后，会自动 fast-forward clean idle sessions。

如果 Alice 有本地改动，“和 main 同步”就等价于 integration。Coconut 不会把最新 `main` 盲目 pull 进 dirty worktree。它会 snapshot 这个 dirty work，把 worktree 重置到最新 `main`，然后要求同一个 Codex 在最新 `main` 上重新实现这份功能。

## 正常例子

Alice 和 Bob 都通过 Coconut 启动 Codex：

```bash
coconut join --name alice -- codex
coconut join --name bob -- codex
```

Alice 让 Codex 实现 feature A。Bob 让 Codex 实现 feature B。两个 session 都变 dirty。

Coconut 把两个 session 入队。假设 Alice 先拿到 lock。Alice 的 Codex 收到 task file，在最新 `main` 上实现 feature A，提交 candidate，然后执行：

```bash
coconut done alice
```

Coconut 验证并发布 Alice 的 candidate，使它成为新的 `main`。

Bob 仍然是 dirty session，所以 Bob 不会直接把之前的 branch 推上去。轮到 Bob 时，Coconut 会 snapshot Bob 的工作，把 Bob 的 worktree 重置到已经包含 feature A 的新 `main`，然后要求 Bob 的 Codex 在这个基础上实现 feature B。Bob 的 Codex 提交后执行：

```bash
coconut done bob
```

这就是 Coconut 提供的串行化保证：feature B 会在 feature A 之后，基于真正的 post-A mainline 重新集成。

## 恢复策略

Coconut 的原则是宁可停下来，也不猜测：

- lock owner 断连时，该 session 会进入 `recovery_required`；
- daemon 在 integration 中途重启时，会保守恢复不完整状态；
- 本地 `main` 如果被 Coconut 以外的过程移动，dirty 或 active session 会进入 `recovery_required`；
- 如果本地 `main` 已经前进但 remote push 失败，Coconut 会保留锁，让同一个 task 在远端问题修复后重试 `coconut done <session>`；
- 如果 verification 失败，task 会进入 `blocked`。

先查看：

```bash
coconut status
coconut log
```

然后显式选择一个动作：

```bash
coconut done alice
coconut block alice "reason"
coconut resume alice
coconut abandon alice
```

## 命令速览

```bash
coconut init --main main --verify "pytest" --remote origin
coconut daemon
coconut join --name alice -- codex
coconut ready alice
coconut status
coconut log
coconut done alice
coconut block alice "reason"
coconut resume alice
coconut abandon alice
```

实现细节请阅读 [docs/DEV_ZH.md](DEV_ZH.md)。
