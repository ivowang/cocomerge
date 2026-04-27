# cocomerge

[English README](../README.md)

cocomerge 用于协调同一个服务器账号下、同一个 Git 仓库里的多个 Codex session。每个开发者都有独立的 managed worktree，Cocomerge 负责把这些 worktree 写入 `main` 的时刻串行化。

## 核心规则

在日常协作中，开发者只需要知道一个 Cocomerge 命令：

```bash
cocomerge sync
```

`sync` 的含义是“把这个 Cocomerge session 推进到下一个安全同步状态”。根据当前状态，它会：

- 将 clean session fast-forward 到最新 `main`；
- 将有本地改动的 session 放入 integration queue；
- 在 Cocomerge 给出 task 且 Codex 已提交 candidate 后，把这个 candidate 发布为新的 `main`。

开发者和 Codex session 不要直接执行 `git pull main`、`git merge main` 或 `git push main`。本地 `main` 只由 Cocomerge 写入。

如果配置了 remote，每次 `cocomerge sync` 还会尝试把这台 server 上的本地 branch refs 强制同步到 remote。Cocomerge 以运行 daemon 的 server 上的 repo 状态为准，remote 分支不一致时可以被覆盖。远程同步是 best-effort：网络或认证失败只会打印 warning，不会中断本地开发，并会在后续 `cocomerge sync` 中重试。

daemon 不会自动集成 dirty session。开发者的本地工作会一直留在自己的 managed worktree 里，直到该开发者或对应 Codex 显式运行：

```bash
cocomerge sync
```

在 Codex 中可以用 shell 命令形式执行，例如 `!cocomerge sync`。

## 角色分工

Operator 负责初始化和启动；这些命令在项目仓库中执行：

```bash
cocomerge init --main main --remote origin
cocomerge daemon
cocomerge join alice
```

开发者协作时使用；这个命令在自己的 managed worktree 中执行，通常是在 Codex 里通过 `!cocomerge sync` 运行：

```bash
cocomerge sync
```

查看状态：

```bash
cocomerge status
cocomerge log
```

`resume` 和 `abandon` 恢复命令只面向 operator，不属于普通开发者工作流。

## 安装

第一次发布到 PyPI 后，可以直接安装：

```bash
pip install cocomerge
```

如果是在本地 checkout 中开发 Cocomerge 本身，执行：

```bash
pip install -e .
```

安装后会提供 `cocomerge` 命令。

## 项目仓库准备

下面的命令要在团队实际开发的项目仓库中执行。

配置给 Cocomerge 的主分支必须已经存在，并且至少有一个 initial commit：

```bash
git switch -c main
git add .
git commit -m "initial commit"
```

如果希望 Cocomerge 为 server 上的本地分支保留远端副本，初始化前先添加 remote：

```bash
git remote add origin <url>
```

初始化 Cocomerge：

```bash
cocomerge init --main main --remote origin
```

`init` 默认拒绝覆盖已有 `.cocomerge/config.json`，因为这个文件里有开发者 identity 和启动命令。只有在明确想替换现有 Cocomerge 配置时，才使用 `cocomerge init --force`。

只有当 `origin` 已经存在时才使用 `--remote origin`。配置 remote 后，`cocomerge sync` 会用 force-push/prune 的方式把本地 branch refs 同步到该 remote，因此以 server 上的仓库为权威状态。如果只需要本地协调，可以省略 `--remote`。

开发者 join 之前，编辑 `.cocomerge/config.json`，填好顶层 `developers` 对象。保留 `cocomerge init` 写入的其他 key，不要只用 developer 片段覆盖整个文件。一个典型配置如下：

```json
{
  "developers": {
    "alice": {
      "git_user_name": "Alice Example",
      "git_user_email": "alice@example.com"
    },
    "bob": {
      "git_user_name": "Bob Example",
      "git_user_email": "bob@example.com"
    }
  },
  "dirty_interval_s": 2.0,
  "main_branch": "main",
  "remote": "origin",
  "socket_path": ".cocomerge/cocomerge.sock",
  "worktree_root": ".cocomerge/worktrees"
}
```

如果只做本地协调，将 `"remote"` 设为 `null`。`developers` 下面的 key 就是 `cocomerge join <user_name>` 接受的名字，所以 `cocomerge join alice` 要求配置中存在 `alice` entry。

每个开发者的 `command` 字段可选；不写时 Cocomerge 默认启动 `codex`。如果需要自定义 Codex 启动方式，可以写 JSON 字符串数组，例如 `"command": ["codex", "--model", "gpt-5.5"]`。

## 启动 Codex Sessions

在项目仓库中，用一个长期运行的终端启动 daemon：

```bash
cocomerge daemon
```

每个 Codex session 都在对应开发者自己的 tmux 窗口中通过 Cocomerge 启动：

```bash
cocomerge join alice
cocomerge join bob
```

第一次加入和之后重新加入都使用同一个命令格式。developer name 来自 `.cocomerge/config.json`；Git identity 和 Codex 启动命令都来自匹配的配置 entry。

每个 joined session 会得到：

- 一个名为 `cocomerge/<name>` 的 branch；
- 一个位于 `.cocomerge/worktrees/<name>` 的 worktree；
- 一个接收 Cocomerge task 的 session agent；
- 一个位于该 worktree 根目录、被 Git 忽略的 `AGENTS.md`，除非项目本身已经有自己的 `AGENTS.md`。

`join` 会从 `.cocomerge/config.json` 读取该开发者的 Git identity，并写入该 worktree 的 per-worktree Git config，因此 Cocomerge snapshot commit 和 Codex candidate commit 都会使用正确作者。

Cocomerge 不会自动推断 tmux pane，因为 `TMUX_PANE` 可能从脚本、测试或嵌套 shell 中泄漏，导致 prompt 被粘贴到错误的 Codex。默认情况下，sync task 开始时 Cocomerge 会在终端打印 task 和 prompt 文件路径。如果希望 Cocomerge 把 sync prompt 直接粘贴进 Codex pane，需要显式 opt in：

```bash
cocomerge join --tmux-target "$TMUX_PANE" alice
```

只有在当前 `join` 命令确实是在承载该开发者 Codex 的同一个 tmux pane 中运行时，才使用 `--tmux-target "$TMUX_PANE"`。

生成的 `AGENTS.md` 会告诉 Codex 它处在 Cocomerge 管理的协作 session 中，并说明正常同步只需要在 managed worktree 中运行 `cocomerge sync`。

## 重启 Session

如果开发者关掉了自己的 Codex 窗口，之后用同一个 session name 重新启动：

```bash
cocomerge join alice
```

Cocomerge 会复用 `.cocomerge/worktrees/alice` 和 `cocomerge/alice`。`join` 启动时会先检查这个 session 是否有遗留的 Cocomerge 责任：

- 如果有 active sync task，会重新提示 task file 和 validation file；
- 如果中断的 task 可以安全恢复，会自动回到 `fusing`；
- 如果已有 sync request 在 queue 中，会提示 Codex 等待 task；
- 如果 clean session 只是落后于 `main`，会安全 fast-forward；
- 如果有尚未集成的本地工作，会提示 Codex 先 review，再决定何时 `cocomerge sync`。

如果出现 restart notice，先处理 notice，再开始新的 feature 开发。显式传入 `--tmux-target` 时，Cocomerge 也会把 restart notice 粘贴进对应 Codex pane。

## `sync` 做什么

### Clean Session

如果 Alice 没有本地工作，而 `main` 已经前进，执行：

```bash
cocomerge sync
```

Cocomerge 会把 Alice fast-forward 到最新 `main`。如果 Alice 已经是最新状态，Cocomerge 会提示已经同步。

### Dirty Session

如果 Alice 有本地修改或本地 commit，执行：

```bash
cocomerge sync
```

这会请求进入 integration queue。轮到 Alice 时，Cocomerge 会：

1. freeze Alice 的 session；
2. snapshot Alice 当前工作；
3. 将 Alice 的 worktree 重置到最新 `main`；
4. 在 `.cocomerge/tasks/` 下写出 task file；
5. 在 Alice 的 Codex 终端里打印 task file 路径。

Alice 的 Codex 读取 task file，在最新 `main` 上重新实现或语义融合 Alice 的 feature。如果这个 task 到来时 Codex 正在处理另一个开发请求，Codex 应该先选择安全暂停点，保留当前请求剩余意图，完成这个 sync task，并在 sync 成功后继续之前暂停的开发工作。

每个 task 都由 Codex 自己为这次语义融合设计并执行充分验证。验证可以包括现有测试、新增或更新测试、定向脚本，或者在项目没有合适测试框架时执行合理的手动检查。再次运行 sync 前，Codex 需要按 task 要求在 `.cocomerge/tasks/` 下写出 validation report。提交最终 candidate 并确保 worktree clean 后，再执行同一个命令：

```bash
cocomerge sync
```

随后 Cocomerge 会要求 validation report 存在、fast-forward 本地 `main`、在配置了 remote 时 best-effort 同步远端，并通知其他 session。

如果 task 无法安全完成，Codex 应该停下来，在 session 输出中说明 blocker。operator 可以通过 `cocomerge status` 和 `cocomerge log` 判断如何恢复。

## 正常例子

Alice 和 Bob 都通过 Cocomerge 启动 Codex。Alice 实现 feature A，Bob 实现 feature B。两个分支都不会被 daemon 自动集成。

Alice 执行：

```bash
!cocomerge sync
```

Cocomerge 给 Alice 的 Codex 一个 task。Alice 的 Codex 在最新 `main` 上实现 feature A，提交后再次执行：

```bash
!cocomerge sync
```

此时 feature A 成为新的 `main`。

之后 Bob 执行：

```bash
!cocomerge sync
```

Bob 的 task 会基于当前 `main`，也就是已经包含 feature A 的主线。Bob 的 Codex 在这个基础上实现 feature B，提交后再次执行：

```bash
!cocomerge sync
```

这样，即使多个 Codex 异步开发，进入 `main` 的过程仍然是串行的。

## 安全策略

Cocomerge 的原则是宁可停下来，也不猜测：

- dirty session 不会被自动集成，必须由 owner 运行 `sync`；
- 同一时间只有一个 session 持有 integration lock；
- task candidate 还没提交时再次运行 `sync` 会被拒绝；
- validation report 缺失或不足时 task 保持锁定，同一个 session 写好 report 后继续运行 `sync`；
- remote sync 失败不会阻塞本地进度；Cocomerge 会打印 warning，并在下一次 `sync` 时重试；
- 非预期 recovery 状态需要 operator 检查。

## 命令速览

普通开发者命令：

```bash
cocomerge sync
```

常用 operator 命令：

```bash
cocomerge init --main main --remote origin
cocomerge daemon
cocomerge join alice
cocomerge status
cocomerge log
```

## 常见问题

`Developer 'alice' is not configured in .cocomerge/config.json` 表示 operator 还没有在 `developers` 下添加 `alice` entry，或者当前命令运行在另一个 Cocomerge config 所属的仓库中。

`cocomerge sync must run inside a Git worktree` 或 `Run cocomerge sync inside a managed worktree` 表示当前不是在 `.cocomerge/worktrees/<name>` 里运行。先用 `cocomerge join <name>` 启动或重新进入对应 session，再从这个 Codex session 中运行 `!cocomerge sync`。

如果 Cocomerge 只打印 task 和 prompt 文件路径，而没有自动粘贴进 Codex，这是正常行为，除非启动 session 时显式传了 `--tmux-target`。在对应 worktree 里读取 task file，然后按 task 执行。

remote sync warning 不会阻断本地开发。之后修复网络或 Git 认证问题即可；Cocomerge 会在后续 `cocomerge sync` 中重试远程同步。

实现细节请阅读 [docs/DEV_ZH.md](DEV_ZH.md)。
