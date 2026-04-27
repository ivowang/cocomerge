# coconut

[English README](../README.md)

coconut 用于协调同一个服务器账号下、同一个 Git 仓库里的多个 Codex session。每个开发者都有独立的 managed worktree，Coconut 负责把这些 worktree 写入 `main` 的时刻串行化。

## 核心规则

在日常协作中，开发者只需要知道一个 Coconut 命令：

```bash
coconut sync
```

`sync` 的含义是“把这个 Coconut session 推进到下一个安全同步状态”。根据当前状态，它会：

- 将 clean session fast-forward 到最新 `main`；
- 将有本地改动的 session 放入 integration queue；
- 在 Coconut 给出 task 且 Codex 已提交 candidate 后，把这个 candidate 发布为新的 `main`。

开发者和 Codex session 不要直接执行 `git pull main`、`git merge main` 或 `git push main`。本地 `main` 只由 Coconut 写入。

daemon 不会自动集成 dirty session。开发者的本地工作会一直留在自己的 managed worktree 里，直到该开发者或对应 Codex 显式运行：

```bash
coconut sync
```

在 Codex 中可以用 shell 命令形式执行，例如 `!coconut sync`。

## 角色分工

Operator 负责初始化和启动：

```bash
coconut init --main main --verify "pytest" --remote origin
coconut daemon
coconut join --name alice \
  --git-user-name "Alice Example" \
  --git-user-email alice@example.com \
  -- codex
```

开发者协作时使用：

```bash
coconut sync
```

查看状态：

```bash
coconut status
coconut log
```

恢复命令只面向 operator，不属于普通开发者工作流。

## 安装

在 Coconut 仓库根目录执行：

```bash
pip install -e .
```

安装后会提供 `coconut` 命令。

## 项目仓库准备

下面的命令要在团队实际开发的项目仓库中执行。

配置给 Coconut 的主分支必须已经存在，并且至少有一个 initial commit：

```bash
git switch -c main
git add .
git commit -m "initial commit"
```

如果希望 Coconut 发布本地 `main` 后推送远端，初始化前先添加 remote：

```bash
git remote add origin <url>
```

初始化 Coconut：

```bash
coconut init --main main --verify "pytest" --remote origin
```

只有当 `origin` 已经存在时才使用 `--remote origin`。如果只需要本地协调，可以省略 `--remote`。

## 启动 Codex Sessions

在项目仓库中，用一个长期运行的终端启动 daemon：

```bash
coconut daemon
```

每个 Codex session 都在对应开发者自己的 tmux 窗口中通过 Coconut 启动：

```bash
coconut join --name alice \
  --git-user-name "Alice Example" \
  --git-user-email alice@example.com \
  -- codex

coconut join --name bob \
  --git-user-name "Bob Example" \
  --git-user-email bob@example.com \
  -- codex
```

每个 joined session 会得到：

- 一个名为 `coconut/<name>` 的 branch；
- 一个位于 `.coconut/worktrees/<name>` 的 worktree；
- 一个接收 Coconut task 的 session agent；
- 一个位于该 worktree 根目录、被 Git 忽略的 `AGENTS.md`，除非项目本身已经有自己的 `AGENTS.md`。

`join` 会把传入的 Git identity 写入该 worktree 的 per-worktree Git config，因此 Coconut snapshot commit 和 Codex candidate commit 都会使用正确作者。如果没有传入 identity，该 worktree 必须已经能读取到有效的 `user.name` 和 `user.email` Git config。

当 `join` 在 tmux 中运行时，Coconut 会自动识别当前 pane，并在 sync task 到来时把 prompt 直接粘贴到这个 pane 里正在运行的 Codex。可以用 `--tmux-target` 显式指定 pane，或用 `--no-auto-prompt` 关闭自动 prompt 注入。不在 tmux 中运行时，Coconut 仍会打印 task 和 prompt 文件路径。

生成的 `AGENTS.md` 会告诉 Codex 它处在 Coconut 管理的协作 session 中，并说明正常同步只需要在 managed worktree 中运行 `coconut sync`。

## `sync` 做什么

### Clean Session

如果 Alice 没有本地工作，而 `main` 已经前进，执行：

```bash
coconut sync
```

Coconut 会把 Alice fast-forward 到最新 `main`。如果 Alice 已经是最新状态，Coconut 会提示已经同步。

### Dirty Session

如果 Alice 有本地修改或本地 commit，执行：

```bash
coconut sync
```

这会请求进入 integration queue。轮到 Alice 时，Coconut 会：

1. freeze Alice 的 session；
2. snapshot Alice 当前工作；
3. 将 Alice 的 worktree 重置到最新 `main`；
4. 在 `.coconut/tasks/` 下写出 task file；
5. 在 Alice 的 Codex 终端里打印 task file 路径。

Alice 的 Codex 读取 task file，在最新 `main` 上重新实现或语义融合 Alice 的 feature。提交最终 candidate 并确保 worktree clean 后，再执行同一个命令：

```bash
coconut sync
```

随后 Coconut 会验证 candidate、fast-forward 本地 `main`、在配置了 remote 时推送远端，并通知其他 session。

如果 task 无法安全完成，Codex 应该停下来，在 session 输出中说明 blocker。operator 可以通过 `coconut status` 和 `coconut log` 判断如何恢复。

## 正常例子

Alice 和 Bob 都通过 Coconut 启动 Codex。Alice 实现 feature A，Bob 实现 feature B。两个分支都不会被 daemon 自动集成。

Alice 执行：

```bash
!coconut sync
```

Coconut 给 Alice 的 Codex 一个 task。Alice 的 Codex 在最新 `main` 上实现 feature A，提交后再次执行：

```bash
!coconut sync
```

此时 feature A 成为新的 `main`。

之后 Bob 执行：

```bash
!coconut sync
```

Bob 的 task 会基于当前 `main`，也就是已经包含 feature A 的主线。Bob 的 Codex 在这个基础上实现 feature B，提交后再次执行：

```bash
!coconut sync
```

这样，即使多个 Codex 异步开发，进入 `main` 的过程仍然是串行的。

## 安全策略

Coconut 的原则是宁可停下来，也不猜测：

- dirty session 不会被自动集成，必须由 owner 运行 `sync`；
- 同一时间只有一个 session 持有 integration lock；
- task candidate 还没提交时再次运行 `sync` 会被拒绝；
- verification 失败时 task 保持锁定，同一个 session 修复后继续运行 `sync`；
- remote push 失败时 task 保持锁定，远端问题修复后继续运行 `sync`；
- 非预期 recovery 状态需要 operator 检查。

## 命令速览

普通开发者命令：

```bash
coconut sync
```

常用 operator 命令：

```bash
coconut init --main main --verify "pytest" --remote origin
coconut daemon
coconut join --name alice --git-user-name "Alice Example" --git-user-email alice@example.com -- codex
coconut status
coconut log
```

实现细节请阅读 [docs/DEV_ZH.md](DEV_ZH.md)。
