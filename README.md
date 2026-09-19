# ApplyOps

**A harness-agnostic MCP server for job applications.** It gives an AI agent the *hands* to fill in real application forms in a real browser, and the *memory* to interrupt you less on every application after the first.

No model, no API key, no agent loop of its own — the brain is your harness.

**New here?** Follow [Zero to first application](#zero-to-first-application) below — every step is copy-pasteable and says what you should see.

[English](#english) · [中文](#中文)

---

<a id="english"></a>

## English

### What it is

ApplyOps is **not** an agent. It is an **MCP server** exposing 32 tools that any MCP-speaking harness — Codex, WorkBuddy, Claude Code — can drive:

| | |
|---|---|
| **Hands** | A real browser that fills real forms, through shadow DOM and cross-origin iframes, plus a locator layer built to survive a page redesign |
| **Memory** | A flywheel recording every question ever answered, every selector that ever worked, and how each application *route* behaves — so application N+1 asks you less than application N |

Everything else is the harness's job: planning, reading a difficult page, handling the unexpected, talking to you.

In plain terms: **you talk to your AI assistant, and ApplyOps is what lets it actually open a browser and submit the form for you.**

<a id="zero-to-first-application"></a>

### Zero to first application

All commands run in a terminal (Terminal.app on macOS). Each step ends with what you should see — if you don't, jump to [Troubleshooting](#troubleshooting).

**Step 0 — Check your Python.** You need Python **3.12 or newer**:

```bash
python3 --version        # needs to say 3.12 or higher
```

**Step 1 — Install `uv`** (a tool that manages Python dependencies for you; it will also install a matching Python if needed):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then **close and reopen your terminal** so the `uv` command is found.

**Step 2 — Get the code and install everything:**

```bash
git clone https://github.com/hanyuli0310/applyops-agent.git
cd applyops-agent

uv sync                                  # creates .venv/ and installs dependencies
uv run playwright install chrome         # installs the real Google Chrome it drives
```

> No `git`? Click **Code → Download ZIP** on the GitHub page instead, unzip it, and `cd` into the folder.

**Step 3 — Tell it who you are.** Run the setup wizard:

```bash
.venv/bin/applyops-init
```

It asks, one question at a time, everything a job form will ever ask — name, email, phone, work authorization, salary expectations, and so on. Three things to know:

- **Enter** keeps the current value, `-` clears one, **Ctrl-C quits anytime** — answers are saved after *every* question, so you can rerun the command later and it resumes instead of restarting.
- Anything you leave blank stays blank, and the system will **ask you when it reaches that field** rather than guess. Salary and visa status are exactly where a confident wrong answer causes real harm.
- Your answers live in `data/profile.md` — an ordinary markdown file you can open and edit by hand at any time. All 34 fields are explained in [`profile.example.md`](profile.example.md).

**Step 4 — Bring your LinkedIn login over.** Easy Apply needs a logged-in session. Reuse the one your Chrome already has:

```bash
.venv/bin/python tools/import_chrome_session.py --list                      # find the Chrome profile you use
.venv/bin/python tools/import_chrome_session.py --domains linkedin.com --verify   # confirm the session came across
```

> Skipped or it failed? Nothing breaks — the first time an application needs it, the browser will simply show a LinkedIn login page and you sign in once by hand.

**Step 5 — Register the server in your AI harness.** This tells your AI assistant that ApplyOps exists. Point it at the `applyops-mcp` command **inside this project** (use the full absolute path):

```json
{
  "mcpServers": {
    "applyops": {
      "command": "/FULL/PATH/TO/applyops-agent/.venv/bin/applyops-mcp"
    }
  }
}
```

- **Claude Code**: `claude mcp add applyops -- /FULL/PATH/TO/applyops-agent/.venv/bin/applyops-mcp`
- **Codex**: add the same command under `[mcp_servers.applyops]` in `~/.codex/config.toml`
- Any other harness: the JSON above, in whatever place it accepts MCP servers.

You can also test the server by itself, without any harness:

```bash
.venv/bin/python tools/op.py ping '{}'            # → the server replies
.venv/bin/python tools/op.py setup_status '{}'    # → is your profile complete?
.venv/bin/python -m pytest tests/ -q              # → 22 tests pass, ~6s
```

**Step 6 — Apply.** Open your AI assistant and just say it in one sentence:

> 帮我投递这个岗位：https://www.linkedin.com/jobs/view/XXXX

That's it. The agent calls `preflight` (daily cap / pacing / have-you-already-applied), `route_guide` (which route this posting takes), opens the real browser, fills the form from your profile and its memory, and shows you a **full summary of every field** before asking you to confirm submission. Nothing is ever submitted without your one-time confirmation.

The first application will ask you a few things the memory doesn't know yet. The second asks fewer. That's the flywheel.

### Troubleshooting

| What you see | What it means | What to do |
|---|---|---|
| `command not found: uv` | The install script finished but your shell hasn't reloaded | Close and reopen the terminal; or run `source ~/.cargo/env` |
| Python says 3.11 or lower | Too old | `uv sync` will fetch its own Python — just make sure you ran it, and use `.venv/bin/python` everywhere |
| `applyops-init` prints questions but Enter does nothing | You're pasting into the wrong window | Run it in a real terminal, not inside some other tool |
| `browser_busy: true` | Another driver (unattended loop, another chat) holds the Chrome profile | Wait for it to finish; the message names the holder. Don't retry in a loop |
| A fill reports `mismatch: true` | The page rejected the value | It is **not** filled — fix the value and refill; never treat it as done |
| LinkedIn login page appears mid-run | Step 4 was skipped or the session expired | Sign in once by hand in the opened browser; it persists in `data/browser-profile/` |
| Server "not found" in the harness | Wrong command path, or not absolute | Use the **absolute** path of `.venv/bin/applyops-mcp` inside this project |

### Daily use

Once the first application has gone through, the two everyday patterns are:

**Keep using it by conversation.** Paste a job URL into your AI assistant. Same flow, fewer questions each time.

**Unattended mode** — a supervised loop that discovers and applies on a schedule:

```bash
.venv/bin/python tools/cron_apply.py --dry-run    # rails + browser only, no applications
.venv/bin/python tools/cron_apply.py --ensure     # start the loop if it is not running
.venv/bin/python tools/cron_apply.py --status     # is it up? how much quota is left?
.venv/bin/python tools/cron_apply.py --stop
```

Run `--dry-run` first, and watch the first real submission — the loop drives the same Chrome profile you do.

### What your data looks like

Everything mutable is under `data/`, which is **git-ignored in full**:

| path | contents | safe to delete? |
|---|---|---|
| `data/profile.md` | your facts, as a hand-editable markdown file | no — that is your work |
| `data/memory.json` | the flywheel: learned answers, selector scores, history, route knowledge | no |
| `data/guard_state.json` | today's count, breaker, outstanding confirmation tokens | yes |
| `data/browser-profile/` | the live Chrome profile, **including session cookies** | yes, then re-import |

The repository itself contains **no personal data of any kind** — no default name, email, phone, salary or location. Keep it that way.

### Why this shape

The first version of this project owned the brain: an agent loop, pluggable LLM providers, a web UI. Moving the brain out removed the API-key requirement, the provider-drift problem, and the entire front end in one stroke. What survives a change of harness is exactly what remains here: the browser layer and the learning memory. `legacy/` holds the retired v1 as a rollback point.

### The invariants

Five rules the code is built around. Breaking one produces something that *looks* like it works.

1. **The flywheel records inside the tools, never in the caller.**
2. **Submission requires a one-time confirmation token** — the only real guard against a mis-filled form.
3. **A blank profile field is asked about, never guessed** — salary, visa status, work authorization and legal declarations are where a confident wrong answer causes real harm.
4. **`selectors_suggested` must be non-zero after real runs** — the only signal separating "never tried" from "tried and always failed".
5. **Human-required gates are declared up front**, in the route, before the form is opened.

### Route knowledge: Easy Apply is the easy case

LinkedIn Easy Apply stays on the posting page. `amazon.jobs` hands you to `passport.amazon.jobs`, where a sign-in — usually confirmed by an emailed one-time code — stands in front of the *first* input field. So knowledge is split into two layers, keyed `<platform>/<route>`:

| layer | question it answers |
|---|---|
| selectors (`platforms`) | how do I locate this element |
| routes (`routes`) | how many steps, what does it need, which step needs a human |

`route_guide(job_url)` returns all of this *before* the form is opened, so human-required gates (one-time codes, captchas, account passwords) get batched into the same question set as the profile answers rather than discovered halfway through. A route record also carries flywheel counters — `blocked_at` says *which gate* a route dies at, which is where the next adapter gets written.

### One browser at a time

Several processes share one Chrome profile, because the logged-in session is the one thing this project cannot rebuild for itself. Two Chromes on one profile do not slow each other down — they **overwrite each other's cookie database**. So the profile has an exclusive lock and there is exactly one driver; a second one is refused with `browser_busy: true` naming the holder.

State files use lock + write — and merge, so that even a misbehaving writer cannot lose data:

| file | mechanism | accuracy |
|---|---|---|
| `guard_state.json` — daily cap, breaker, tokens | lock + read-modify-write | **exact**: 12 processes racing a cap of 5, exactly 5 get through |
| `memory.json` — the flywheel | lock + **merge**-on-write | no record lost; counters take the max |
| `application_log.json` — the ledger | same lock, merged by posting | no row lost or duplicated |

Locks are `flock`, not pid files: the kernel releases them however a process dies (`SIGKILL` included), so there is no stale-lock cleanup anywhere and no code that has to answer "is that pid still alive". `tests/test_concurrency.py` genuinely spawns processes to prove all of the above.

### License

MIT — see [LICENSE](LICENSE).

---

<a id="中文"></a>

## 中文

### 这是什么

ApplyOps **不是** agent。它是一个 **MCP server**，对外暴露 32 个工具，由任何会说 MCP 的 harness 驱动 —— Codex、WorkBuddy、Claude Code 都行：

| | |
|---|---|
| **手** | 真实浏览器填真实表单，能穿 shadow DOM 和跨域 iframe；定位层按「扛得住页面改版」来写 |
| **记忆** | 飞轮：记下答过的每个问题、生效过的每个选择器、以及每条投递*路由*的行为 —— 所以第 N+1 次投递比第 N 次更少打扰你 |

其余的归 harness：规划、看难页、处理意外、跟你对话。

说人话就是：**你跟你的 AI 助手对话，ApplyOps 让它真的能打开浏览器、替你把表单填好提交。**

<a id="zero-to-first-application-zh"></a>

### 从零到第一次投递

以下命令都在终端（macOS 的「终端」App）里执行。每一步都写了「你应该看到什么」，看不到就跳到[常见问题](#常见问题)。

**第 0 步 —— 检查 Python。** 需要 **3.12 或更新**：

```bash
python3 --version        # 要显示 3.12 或更高
```

**第 1 步 —— 安装 `uv`**（帮你管理 Python 依赖的工具，缺 Python 时它还能自动装一个）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

装完后**关掉终端重新打开**，`uv` 命令才能被找到。

**第 2 步 —— 拿到代码，装好一切：**

```bash
git clone https://github.com/hanyuli0310/applyops-agent.git
cd applyops-agent

uv sync                                  # 建出 .venv/ 并安装依赖
uv run playwright install chrome         # 安装它要驱动的真实 Chrome
```

> 不会用 `git`？在 GitHub 页面点 **Code → Download ZIP**，解压后 `cd` 进文件夹就行。

**第 3 步 —— 告诉它你是谁。** 运行设置向导：

```bash
.venv/bin/applyops-init
```

它会把填表可能问到的一切逐项问你：姓名、邮箱、电话、工作授权、期望薪资……三件事要知道：

- **直接回车** = 保留当前值，输入 `-` = 清空这一项，**Ctrl-C 随时退出** —— 每答完一题就立刻保存，所以中途退出再跑会**接着来**，不用从头答。
- 你留空的就真的是空的，系统到那个字段时会**来问你**，绝不替你猜。薪资、签证状态正是「自信地答错会造成实害」的地方。
- 答案写在 `data/profile.md` —— 一个普通的 markdown 文件，随时可以打开手改。全部 34 个字段的解释见 [`profile.example.md`](profile.example.md)。

**第 4 步 —— 把 LinkedIn 登录态搬过来。** Easy Apply 需要已登录的会话，直接复用你 Chrome 里现成的那个：

```bash
.venv/bin/python tools/import_chrome_session.py --list                      # 找出你在用的 Chrome profile
.venv/bin/python tools/import_chrome_session.py --domains linkedin.com --verify   # 确认登录态搬过来了
```

> 没做这一步或者失败了？不影响 —— 第一次真投递时浏览器会停在 LinkedIn 登录页，你手动登录一次就好。

**第 5 步 —— 在你的 AI harness 里注册服务。** 这一步是告诉你的 AI 助手「ApplyOps 存在」。把它的启动命令（**绝对路径**）注册进去：

```json
{
  "mcpServers": {
    "applyops": {
      "command": "/完整/路径/applyops-agent/.venv/bin/applyops-mcp"
    }
  }
}
```

- **Claude Code**：`claude mcp add applyops -- /完整/路径/applyops-agent/.venv/bin/applyops-mcp`
- **Codex**：把同样的命令写进 `~/.codex/config.toml` 的 `[mcp_servers.applyops]`
- 其他 harness：上面那段 JSON，放到它接受 MCP server 的地方。

不接 harness 也能单独验证服务本身：

```bash
.venv/bin/python tools/op.py ping '{}'            # → 服务应答
.venv/bin/python tools/op.py setup_status '{}'    # → 档案齐了没
.venv/bin/python -m pytest tests/ -q              # → 22 项测试通过，约 6 秒
```

**第 6 步 —— 投递。** 打开你的 AI 助手，一句话：

> 帮我投递这个岗位：https://www.linkedin.com/jobs/view/XXXX

就是这样。agent 会先跑 `preflight`（每日上限 / 节流 / 查重）、`route_guide`（这条招聘走哪条路），然后打开真实浏览器，用你的档案和它的记忆填表，并在请求你确认前**把每个字段的值完整列给你看**。没有你的一次性确认，任何东西都不会被提交。

第一次投递会问你几个记忆里还没有的问题，第二次就更少 —— 这就是飞轮。

<a id="常见问题"></a>

### 常见问题

| 看到什么 | 意味着什么 | 怎么办 |
|---|---|---|
| `command not found: uv` | 安装成功了但 shell 没刷新 | 关掉终端重开；或 `source ~/.cargo/env` |
| Python 显示 3.11 或更低 | 版本太老 | 直接跑 `uv sync`，它会自己装一个合适的 Python —— 之后统一用 `.venv/bin/python` |
| `applyops-init` 在提问但按回车没反应 | 你把命令敲进了别的工具的窗口 | 用真正的终端跑它 |
| 返回 `browser_busy: true` | 另一个驱动（无人值守循环、另一个会话）占着 Chrome profile | 等它跑完；报错里写了持有者是谁，别循环重试 |
| 某次填写返回 `mismatch: true` | 页面拒绝了这个值 | 它**没**填进去 —— 改值重填，永远别当它已填好 |
| 跑到一半出现 LinkedIn 登录页 | 第 4 步没做或会话过期 | 在打开的浏览器里手动登录一次，会话会留在 `data/browser-profile/` |
| harness 里找不到服务 | 命令路径不对，或不是绝对路径 | 用项目里 `.venv/bin/applyops-mcp` 的**绝对路径** |

### 日常使用

第一次投递跑通后，日常就两种用法：

**继续用对话。** 把岗位链接丢给 AI 助手，流程同上，问题一次比一次少。

**无人值守** —— 一个按节奏自动发现岗位并投递的后台循环：

```bash
.venv/bin/python tools/cron_apply.py --dry-run    # 只跑护栏 + 浏览器，不投任何岗位
.venv/bin/python tools/cron_apply.py --ensure     # 没在跑就起起来
.venv/bin/python tools/cron_apply.py --status     # 在跑吗？还剩多少名额？
.venv/bin/python tools/cron_apply.py --stop
```

先跑 `--dry-run`，第一次真实投递建议盯屏 —— 它和你共用同一个 Chrome profile。

### 你的数据长什么样

所有会变的东西都在 `data/` 下，而这个目录**整个被 git 忽略**：

| 路径 | 内容 | 能删吗？ |
|---|---|---|
| `data/profile.md` | 你的个人信息，可手改的 markdown | 不能 —— 这是你的心血 |
| `data/memory.json` | 飞轮：学到的问答、选择器得分、历史、路由知识 | 不能 |
| `data/guard_state.json` | 今日计数、熔断、未消费的确认令牌 | 能 |
| `data/browser-profile/` | 活的 Chrome profile，**含会话 cookie** | 能，删了重新导入即可 |

仓库本身**不含任何人的个人信息** —— 没有默认的名字、邮箱、电话、薪资、地址。请保持这样。

### 为什么是这个形态

这个项目的第一版自带大脑：agent 循环、可插拔 LLM provider、Web UI。把大脑搬出去后，API Key 要求、provider 漂移、整个前端一次性消失。换任何 harness 都不会失去的，恰好就是这里剩下的：浏览器层和学习型记忆。`legacy/` 是退役的 v1，作为回滚点保留。

### 五条不变式

代码就是围着这五条写的。破掉任何一条，都会得到一个**看起来能用**的系统。

1. **飞轮记录在工具内部，不在调用方。**
2. **提交必须持有一次性确认令牌** —— 这是拦住「误提交」的唯一真护栏。
3. **档案空值只能问，不能猜** —— 薪资、签证状态、工作授权、法律声明，答错的代价是实害。
4. **`selectors_suggested` 在真实跑单后必须非零** —— 它是唯一能区分「从没试过」和「试了全失败」的信号。
5. **人工关卡必须提前声明** —— 写在路由里，在开表单之前就拿到。

### 申请路由经验库：Easy Apply 才是简单的那条

LinkedIn Easy Apply 不出招聘页；`amazon.jobs` 会把你交给 `passport.amazon.jobs`，那里在**第一个输入框之前**就横着一道登录，而且往往要邮箱验证码。所以知识分两层，key 都是 `<平台>/<路由>`：

| 层 | 回答的问题 |
|---|---|
| 选择器（`platforms`） | 这个元素怎么定位 |
| 路由（`routes`） | 几步、前提是什么、哪一步必须人来做 |

`route_guide(job_url)` 在**开表单之前**返回这些，所以人工关卡（验证码、一次性密码、建账号）能和档案问题一起批量问掉，而不是填到一半才发现。路由记录还带飞轮计数 —— `blocked_at` 说「死在哪道闸门」，下一个适配器就从这句话写起。

### 一次只开一个浏览器

多个进程共用一个 Chrome profile，因为登录态是这个项目唯一自己造不出来的东西。两个 Chrome 挤一个 profile 不是「慢一点」，是**互相重写 cookie 数据库**。所以 profile 有独占锁，同一时刻只有一个驱动；第二个会被拒绝，返回 `browser_busy: true` 并报出持有者是谁。

状态文件用锁 + 写入 —— 而且是**合并写**，即使某个写者不守规矩也不丢数据：

| 文件 | 机制 | 精确度 |
|---|---|---|
| `guard_state.json` —— 每日上限 / 熔断 / 令牌 | 锁 + 读—改—写 | **精确**：12 个进程抢 5 个名额，结果就是 5 |
| `memory.json` —— 飞轮 | 锁 + **合并写** | 记录一条不丢；计数器取 max |
| `application_log.json` —— 台账 | 同一把锁，按 posting 合并 | 一条不丢、不重复 |

锁用 `flock`，不用 pid 文件：内核在进程以任何方式退出时释放锁（含 `SIGKILL`），所以项目里没有陈旧锁清理，也没有任何代码需要回答「那个 pid 还活着吗」。`tests/test_concurrency.py` **真的** spawn 多进程验证以上全部。

### 许可

MIT，见 [LICENSE](LICENSE)。
