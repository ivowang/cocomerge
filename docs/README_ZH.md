# cocodex

[English README](../README.md)

cocodex 用于协调同一个服务器账号下、同一个 Git 仓库里的多个 Codex session。每个开发者都有独立的 managed worktree，Cocodex 负责把这些 worktree 写入 `main` 的时刻串行化。

## 核心规则

在日常协作中，开发者只需要知道一个 Cocodex 命令：

```bash
cocodex sync
```

`sync` 的含义是“把这个 Cocodex session 推进到下一个安全同步状态”。根据当前状态，它会：

- 将 clean session fast-forward 到最新 `main`；
- 当 dirty session 已经基于最新 `main` 时直接发布；
- 当 dirty session 和 `main` 都有更新时，先在 lock 内尝试普通 Git merge
  并执行轻量结构检查，成功则直接发布；
- 当 Git 不能 clean merge 或轻量检查失败时，再启动语义融合；
- 在 Cocodex 给出 task 且 Codex 已提交 candidate 后，把这个 candidate 发布为新的 `main`。

开发者和 Codex session 不要直接执行 `git pull main`、`git merge main` 或 `git push main`。本地 `main` 只由 Cocodex 写入。
Cocodex 会安装本地 Git hooks，阻止普通 Git 命令直接提交、cherry-pick、rebase、更新或 push `main`；有意维护操作不属于普通开发者流程。

如果配置了 remote，每次 `cocodex sync` 还会尝试把本地 `main` 和当前 session branch 强制同步到 remote。它不会 push 或 prune 其他开发者的 branch。远程同步是 best-effort：网络或认证失败只会打印 warning，不会中断本地开发，并会在后续 `cocodex sync` 中重试。

daemon 不会自动集成 dirty session。开发者的本地工作会一直留在自己的 managed worktree 里，直到该开发者或对应 Codex 显式运行：

```bash
cocodex sync
```

在 Codex 中可以用 shell 命令形式执行，例如 `!cocodex sync`。

## 角色分工

Operator 负责初始化和启动；这些命令在项目仓库中执行：

```bash
cocodex init --main main --remote origin
cocodex daemon
cocodex join alice
```

开发者协作时使用；这个命令在自己的 managed worktree 中执行，通常是在 Codex 里通过 `!cocodex sync` 运行：

```bash
cocodex sync
```

查看状态：

```bash
cocodex status
cocodex log
cocodex task alice
```

`resume` 和 `abandon` 恢复命令只面向 operator，不属于普通开发者工作流。

## 安装

第一次发布到 PyPI 后，可以直接安装：

```bash
pip install cocodex
```

如果是在本地 checkout 中开发 Cocodex 本身，执行：

```bash
pip install -e .
```

安装后会提供 `cocodex` 命令。

## 项目仓库准备

下面的命令要在团队实际开发的项目仓库中执行。

配置给 Cocodex 的主分支必须已经存在，并且至少有一个 initial commit：

```bash
git switch -c main
git add .
git commit -m "initial commit"
```

如果希望 Cocodex 为 server 上的本地分支保留远端副本，初始化前先添加 remote：

```bash
git remote add origin <url>
```

初始化 Cocodex：

```bash
cocodex init --main main --remote origin
```

`init` 默认拒绝覆盖已有 `.cocodex/config.json`，因为这个文件里有开发者 identity 和启动命令。只有在明确想替换现有 Cocodex 配置时，才使用 `cocodex init --force`。

只有当 `origin` 已经存在时才使用 `--remote origin`。配置 remote 后，`cocodex sync` 会 force-push 本地 `main` 和当前 session branch 到该 remote。如果只需要本地协调，可以省略 `--remote`。

`init` 还会安装 Cocodex 管理的 Git hooks，并把 `/.cocodex/` 写入仓库本地的 `.git/info/exclude`。这些 hook 会阻止普通直接写入或 push `main` 的 Git 操作；Cocodex 自己的 publish 路径会使用内部 bypass。

开发者 join 之前，编辑 `.cocodex/config.json`，填好顶层 `developers` 对象。保留 `cocodex init` 写入的其他 key，不要只用 developer 片段覆盖整个文件。一个典型配置如下：

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
  "socket_path": ".cocodex/cocodex.sock",
  "worktree_root": ".cocodex/worktrees"
}
```

如果只做本地协调，将 `"remote"` 设为 `null`。`developers` 下面的 key 就是 `cocodex join <user_name>` 接受的名字，所以 `cocodex join alice` 要求配置中存在 `alice` entry。

每个开发者的 `command` 字段可选；不写时 Cocodex 默认启动 `codex`。如果需要自定义 Codex 启动方式，可以写 JSON 字符串数组，例如 `"command": ["codex", "--model", "gpt-5.5"]`。

## 启动 Codex Sessions

在项目仓库中，用一个长期运行的终端启动 daemon：

```bash
cocodex daemon
```

daemon 会在这个终端中打印运行日志，包括 session join、sync 请求、task 启动、busy sync 拒绝、integration lock 变化、publish 事件、remote sync 失败和 recovery 状态变化。

每个 Codex session 都在对应开发者自己的 tmux 窗口中通过 Cocodex 启动：

```bash
cocodex join alice
cocodex join bob
```

第一次加入和之后重新加入都使用同一个命令格式。developer name 来自 `.cocodex/config.json`；Git identity 和 Codex 启动命令都来自匹配的配置 entry。

每个 joined session 会得到：

- 一个名为 `cocodex/<name>` 的 branch；
- 一个位于 `.cocodex/worktrees/<name>` 的 worktree；
- 一个接收 Cocodex task 的 session agent；
- 一个位于该 worktree 根目录、被 Git 忽略的 `AGENTS.md`，除非项目本身已经有自己的 `AGENTS.md`。

`join` 会从 `.cocodex/config.json` 读取该开发者的 Git identity，并写入该 worktree 的 per-worktree Git config，因此 Cocodex snapshot commit 和 Codex candidate commit 都会使用正确作者。

Cocodex 假设 `join` 是在该开发者自己的 tmux pane 中执行的。当环境里存在 `TMUX_PANE` 时，`join` 会自动把 session agent 绑定到这个 pane，因此 sync task 和 restart notice 会作为普通用户 prompt 被粘贴进正在运行的 Codex。

高级用法中也可以显式覆盖目标 pane：

```bash
cocodex join --tmux-target "$TMUX_PANE" alice
```

如果 `join` 不是在 tmux 中运行，Cocodex 会打印 task 和 prompt 文件路径。此时开发者需要在对应 session worktree 中打开 task file，并按 task 手动执行。

生成的 `AGENTS.md` 会告诉 Codex 它处在 Cocodex 管理的协作 session 中，并说明正常同步只需要在 managed worktree 中运行 `cocodex sync`。

## 重启 Session

如果开发者关掉了自己的 Codex 窗口，之后用同一个 session name 重新启动：

```bash
cocodex join alice
```

Cocodex 会复用 `.cocodex/worktrees/alice` 和 `cocodex/alice`。`join` 启动时会先检查这个 session 是否有遗留的 Cocodex 责任：

- 如果有 active sync task，会重新提示 task file 和 validation file；
- 如果中断的 task 可以安全恢复，会自动回到 `fusing`；
- 如果启动窗口中断时已有 sync request 在 queue 中，会提示 Codex 等待 task；
- 如果 clean session 只是落后于 `main`，会提示这一点，但不会移动 worktree；
- 如果有尚未集成的本地工作，会提示 Codex 先 review，再决定何时 `cocodex sync`。

如果出现 restart notice，先处理 notice，再开始新的 feature 开发。在正常 tmux 工作流里，Cocodex 会自动把 notice 粘贴进 Codex pane。

## `sync` 做什么

### Clean Session

如果 Alice 没有本地工作，而 `main` 已经前进，执行：

```bash
cocodex sync
```

Cocodex 会把 Alice fast-forward 到最新 `main`。如果 Alice 已经是最新状态，Cocodex 会提示已经同步。

### 基于当前 main 的 Dirty Session

如果 Alice 有本地修改或本地 commit，并且 `main` 从 Alice 上次同步后没有前进，执行：

```bash
cocodex sync
```

Cocodex 会直接发布 Alice 当前 worktree。如果 worktree 里有未提交修改，Cocodex 会用 Alice 配置好的 Git identity 创建一个 snapshot commit，然后将本地 `main` fast-forward 到这个 commit，并在配置了 remote 时 best-effort 同步远端。因为没有更新的主干内容需要合并，这条路径不会创建 Codex fusion task。

### Main 已前进后的 Dirty Session

如果 Alice 有本地修改或本地 commit，而 `main` 从 Alice 上次同步后已经前进，`cocodex sync` 只会在没有其他 session 正在 sync 时启动 integration。如果其他 session 持有 integration lock，这次命令会以 `integration busy` 失败；Alice 保留自己的 worktree，等对方完成后再次运行 `cocodex sync`。当 lock 空闲时，Cocodex 会：

1. freeze Alice 的 session；
2. snapshot Alice 当前工作；
3. 尝试把最新 `main` 普通 Git merge 到 Alice 的 snapshot 上；
4. 执行轻量检查：worktree 必须 clean，candidate 必须同时包含最新 `main`
   和 Alice 的 snapshot，并且 candidate diff 必须通过 `git diff --check`；
5. 如果 merge 和检查都成功，直接把这个 merge commit 发布为新的 `main`。

如果 Git 出现 conflict、留下 unsafe state，或者轻量检查失败，Cocodex 会将 Alice 的 worktree 重置到最新 `main`，在 `.cocodex/tasks/` 下写出 task file，并在 tmux 可用时把 sync prompt 粘贴进 Alice 的 Codex 终端。Alice 的 Codex 再读取 task file，在最新 `main` 上重新实现或语义融合 Alice 的 feature。如果这个 task 到来时 Codex 正在处理另一个开发请求，Codex 应该先选择安全暂停点，保留当前请求剩余意图，完成这个 sync task，并在 sync 成功后继续之前暂停的开发工作。

每个 task 都由 Codex 自己为这次语义融合设计并执行充分验证。验证可以包括现有测试、新增或更新测试、定向脚本，或者在项目没有合适测试框架时执行合理的手动检查。再次运行 sync 前，Codex 需要按 task 要求在 `.cocodex/tasks/` 下写出 validation report。提交最终 candidate 并确保 worktree clean 后，再执行同一个命令：

```bash
cocodex sync
```

随后 Cocodex 会要求 validation report 存在、fast-forward 本地 `main`，并在配置了 remote 时 best-effort 同步远端。其他 session worktree 不会在这次 publish 中被移动或通知。

如果 task 无法安全完成，Codex 应该停下来，在 session 输出中说明 blocker。operator 可以通过 `cocodex status`、`cocodex task <name>` 和 `cocodex log` 判断如何恢复。

## 正常例子

Alice 和 Bob 都通过 Cocodex 启动 Codex。Alice 实现 feature A，Bob 实现 feature B。两个分支都不会被 daemon 自动集成。

Alice 执行：

```bash
!cocodex sync
```

如果从 Alice 上次同步之后没有其他人推进 `main`，Cocodex 会直接发布 Alice 当前 worktree。如果 `main` 已经前进，Cocodex 会先在 integration lock 内尝试普通 Git merge。若 merge 和轻量检查成功，Cocodex 会直接发布，不打扰 Codex；只有 Git 不能 clean merge 或检查失败时，Cocodex 才会给 Alice 的 Codex 一个 task。Alice 的 Codex 在最新 `main` 上实现 feature A，提交后再次执行：

```bash
!cocodex sync
```

此时 feature A 成为新的 `main`。

之后 Bob 执行：

```bash
!cocodex sync
```

因为 Alice 已经推进了 `main`，Bob 会收到基于当前 `main` 的 task，也就是已经包含 feature A 的主线。Bob 的 Codex 在这个基础上实现 feature B，提交后再次执行：

```bash
!cocodex sync
```

这样，即使多个 Codex 异步开发，进入 `main` 的过程仍然是串行的。

如果 Bob 在 Alice 的 task 还没完成时运行 `!cocodex sync`，Cocodex 会用 `integration busy` 拒绝 Bob 这次命令。Bob 的 worktree 不会被移动；等 Alice 完成后，Bob 再运行一次 `!cocodex sync`。

## 安全策略

Cocodex 的原则是宁可停下来，也不猜测：

- dirty session 不会被自动集成，必须由 owner 运行 `sync`；
- 一个 session 的 `sync` 不会 fast-forward 另一个 session 的 worktree；
- remote sync 只会 force-push 本地 `main` 和当前 session branch；
- 同一时间只有一个 session 持有 integration lock；
- 当已有 session 正在 sync 时，第二个 session 的 `sync` 会被拒绝；
- 创建 Codex 语义 task 前，会先在同一个 lock 内尝试 clean Git merge；
- 本地 Git hooks 会阻止普通直接写入或 push `main` 的操作；
- task candidate 还没提交时再次运行 `sync` 会被拒绝；
- validation report 缺失或不足时 task 保持锁定，同一个 session 写好 report 后继续运行 `sync`；
- remote sync 失败不会阻塞本地进度；Cocodex 会打印 warning，并在下一次 `sync` 时重试；
- 非预期 recovery 状态需要 operator 检查。

## 恢复与 Resume

先运行 `cocodex status`。它会显示 daemon/session 版本、main guard 状态、每个 session 的 state、active task、blocked reason、branch head、当前配置的 remote，以及 integration lock 是否被持有。用 `cocodex task <name>` 可以查看某个 session 的 active task file、validation file、snapshot ref 和 base ref。

## 失败处理流程

当 Cocodex 命令失败时，不要立刻手动运行 Git recovery 命令。先保持相关 worktree 原样，按下面顺序处理：

1. 先读失败输出。新版 Cocodex 会打印 `Cocodex failure handling`，里面给出下一步安全动作。
2. 在项目仓库中运行 `cocodex status`，确认受影响的 session、state、active task、lock owner，以及是否有版本不一致。
3. 如果 session 有 active task，运行 `cocodex task <name>`，检查 task file、validation file、snapshot ref 和 base ref。
4. 判断是同一个开发者 session 可以继续处理，还是需要 operator 介入。
5. 只有当下一步明确后，才运行 `cocodex sync`、`cocodex resume <name>` 或 `cocodex abandon <name>`。

常见情况：

- `integration busy`：不要移动当前 worktree；等当前 lock owner 完成后，在同一个 worktree 中重试 `cocodex sync`。
- active task 因 candidate 缺失、worktree dirty 或 validation report 缺失而 blocked：同一个 Codex session 修复 task，然后再次运行 `cocodex sync`。
- 无 active task 的 `blocked`：operator 修复外部 blocker 后，在项目仓库运行 `cocodex resume <name>`。
- `recovery_required`：operator 先检查 `cocodex status`、`cocodex log` 和 `cocodex task <name>`，再决定 resume 或 abandon。
- `version mismatch`：升级安装包后，重启该开发者的 `cocodex join <name>`。
- remote sync warning：本地 publish 已完成；之后修复网络或 Git 认证，让后续 `cocodex sync` 自动重试。
- `Cocodex protects main`：Git hook 阻止了直接操作 `main`。继续在 managed worktree 中开发，并通过 `cocodex sync` 发布。

不要把 `abandon` 当成失败后的第一反应。`abandon` 用于明确要丢弃某个 task 或完全手动恢复的场景；它会在清理 Cocodex bookkeeping 前创建 backup ref，但仍然应该由 operator 决定。

普通 task block 不要马上 `resume`。如果某个 session 是带 task id 的
`blocked`，原因是 candidate 没提交、validation 前 worktree 仍然 dirty，
或者 validation report 缺失，那么应该由这个 session 自己的 Codex 在同一个
managed worktree 中修复问题，然后再次运行 `cocodex sync`。这时 integration
lock 仍属于这个 task，其他 session 不能越过它发布。

当 `status` 显示某个 session 是 `blocked` 或 `recovery_required`，并且需要 operator 介入时，使用：

```bash
cocodex resume <name>
```

这是 operator 在项目仓库中执行的恢复命令，不是普通开发者日常命令。如果 session 有 active task，`resume` 会在 integration lock 下恢复该 task，并在 session agent 已连接时重新提示任务。如果 session 没有 active task，先修复底层 blocker，再 resume。例如 direct publish 失败是因为项目仓库的 main worktree 中有会被覆盖的本地文件，那就先在 main worktree 中清理或移动这些文件，然后执行：

```bash
cocodex resume alice
```

无 active task 的 resume 后，Cocodex 会让 daemon 在可处理时重试这个 session。
如果这个开发者的 Codex 窗口已经关闭，之后用同一个名字重新启动：

```bash
cocodex join alice
```

只有当某个 active Cocodex task 应该被丢弃，或者要完全手动恢复时，才使用：

```bash
cocodex abandon <name>
```

`abandon` 只会清理 Cocodex 对这个 session 的 queue/task/lock 记录，不会替你 revert session worktree 里的文件或 commit。清理前它会创建并打印 `refs/cocodex/backups/...` 下的 backup ref。

正常运行时，项目仓库的 main worktree 应保持 clean。开发者改动应该发生在
`.cocodex/worktrees/<name>` 中。main worktree 里的未提交文件可能阻止 Cocodex
fast-forward 本地 `main`。

## 命令速览

普通开发者命令：

```bash
cocodex sync
```

常用 operator 命令：

```bash
cocodex init --main main --remote origin
cocodex daemon
cocodex join alice
cocodex status
cocodex log
cocodex task alice
cocodex resume alice
cocodex abandon alice
```

## 常见问题

`Developer 'alice' is not configured in .cocodex/config.json` 表示 operator 还没有在 `developers` 下添加 `alice` entry，或者当前命令运行在另一个 Cocodex config 所属的仓库中。

`cocodex sync must run inside a Git worktree` 或 `Run cocodex sync inside a managed worktree` 表示当前不是在 `.cocodex/worktrees/<name>` 里运行。先用 `cocodex join <name>` 启动或重新进入对应 session，再从这个 Codex session 中运行 `!cocodex sync`。

如果 Cocodex 只打印 task 和 prompt 文件路径，而没有自动粘贴进 Codex，通常说明 `join` 不是从 tmux pane 中启动的，或者 tmux prompt injection 失败了。可以在对应 worktree 里读取 task file 手动执行，也可以从该开发者的 tmux pane 重新运行 `cocodex join <name>`。

remote sync warning 不会阻断本地开发。之后修复网络或 Git 认证问题即可；Cocodex 会在后续 `cocodex sync` 中重试远程同步。

`integration busy: <name> is syncing task ...` 表示另一个 session 正在持有 integration lock 或即将收到 sync task。保持当前 worktree 不变，等对方完成后再次执行 `!cocodex sync`。

`Cocodex protects main` 表示 Git hook 阻止了直接写入或 push `main`。开发者改动应在 `.cocodex/worktrees/<name>` 中完成，并通过 `cocodex sync` 发布。

`version mismatch` 表示 daemon 和某个仍在运行的 `cocodex join` agent 来自不同 Cocodex 版本。升级安装包后，停止并重新执行该开发者的 `cocodex join`。

如果本地 `main` 已经前进，但 Git 远端一直没有变化，先看 `cocodex status`
和 `.cocodex/config.json`。Cocodex 只有在 `remote` 已配置时才会 push，例如
`"remote": "origin"`。即使 Git 仓库本身有 `origin` remote，如果初始化时没有
传 `--remote origin`，Cocodex 仍会显示 `remote: none`，也不会同步远端；这时
需要编辑配置，或明确重新初始化配置后再期待 remote sync。

`sync already in progress (publishing)` 应该只是短暂状态。如果它持续存在，先
看 `cocodex status` 和 `cocodex log`。如果某个 session 已经是无锁的
`blocked`，通常需要 operator 修复日志里的 blocker 后执行
`cocodex resume <name>`。如果 daemon crash 后出现无锁的 `publishing`，重启
daemon，让启动恢复逻辑把它转成 `recovery_required` 后再处理。

实现细节请阅读 [docs/DEV_ZH.md](DEV_ZH.md)。
