# ApplyOps

**A harness-agnostic MCP server for job applications.** It gives an AI agent the *hands* to fill in real application forms in a real browser, and the *memory* to interrupt you less on every application after the first.

No model, no API key, no agent loop of its own — the brain is your harness.

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

### Why this shape

The first version of this project owned the brain: an agent loop plus three pluggable LLM providers plus a web UI. The problem was not the code quality, it was the **placement**:

| The old design's burden | After moving the brain out |
|---|---|
| It called a model, so it needed an API key | Gone — the harness owns the brain |
| Three LLM adapters, plus model-retirement drift | Entire layer deleted |
| An agent loop, so it spun without a timeout | MCP is request/response: no loop, no spinning |
| A hand-built web UI | The MCP client *is* the front end |

What is left are the only two things that survive a change of harness: the browser layer and the learning memory. `legacy/` holds the retired v1 as a rollback point.

### Requirements

- macOS or Linux, Python **≥ 3.12**
- [`uv`](https://docs.astral.sh/uv/)
- **Google Chrome** (installed by Playwright — not bundled Chromium; importing a LinkedIn session needs a real Chrome profile)
- A **LinkedIn account** for the Easy Apply path

### Quick start

```bash
git clone git@github.com:hanyuli0310/applyops-agent.git
cd applyops-agent

uv sync                                  # creates .venv/
uv run playwright install chrome

.venv/bin/applyops-init                  # answers every question a form will ask

.venv/bin/python tools/import_chrome_session.py --list     # find your logged-in Chrome profile
.venv/bin/python tools/import_chrome_session.py --verify   # confirm the session came across

.venv/bin/applyops-mcp                   # serve over stdio; register this in your harness
```

Then check it works before trusting it:

```bash
.venv/bin/python tools/op.py ping '{}'            # server reachable
.venv/bin/python tools/op.py setup_status '{}'    # profile complete?
.venv/bin/python -m pytest tests/ -q              # 22 tests, ~6s
```

`tools/op.py <tool> '<json>'` calls exactly one tool from the shell, against the real server with all the real guardrails. It is the fastest way to drive a single step by hand.

### First run

Nothing needs configuring by hand. On the first `setup_status()` call the server reports the profile as incomplete and returns a `questionnaire`; the harness asks you those questions in one batch and writes the answers back. If you would rather answer them in a terminal, `applyops-init` walks the same fields — both paths are generated from a single field definition, so they cannot drift apart.

Answers that the system cannot know stay blank. **A blank field means "you have not said", and every layer is forbidden from guessing.** Salary, visa status, work authorization and legal declarations are exactly the cases where a confident wrong answer causes real harm.

### Your profile

Your facts live in one hand-editable markdown file, `data/profile.md`:

```markdown
## Identity

- name: Jane Doe
- email: you@example.com
- phone:  <!-- What is your mobile number, with country code? -->
```

- `- field: value`, one per line. An empty value followed by an HTML comment is a question the system will ask you.
- Edits take effect immediately; no command needs re-running.
- All 34 fields, each with an explanation of *why it is asked*, are documented in **`profile.example.md`**.

It is a separate file from `memory.json` on purpose. `memory.json` holds what the system **learned** — it must keep accumulating and should not be hand-edited. The profile holds what only **you** know, and you will want to correct it. Two files, two lifecycles.

Both live under `data/`, which is git-ignored in full — so your resume can go there too and cannot be committed by accident.

### The invariants

Five rules the code is built around. Breaking one produces something that looks like it works.

1. **The flywheel records inside the tools, never in the caller.** `fill_field` has no three-argument form. If recording lived in the caller, a different agent loop would silently starve the memory while `memory.json` still looked healthy.
2. **Submission requires a one-time confirmation token.** No token, no submission — the only place that actually catches a mis-filled form.
3. **A blank profile field is asked about, never guessed.** See above.
4. **`selectors_suggested` must be non-zero after real runs.** It is the only signal separating "never tried" from "tried and always failed" — both show `hit_rate: 0`. A permanently zero value means the memory is dead.
5. **Human-required gates are declared up front**, in the route, before the form is opened.

### Route knowledge: Easy Apply is the easy case

Selector memory answers "what does the button look like", and silently assumes the form is on the page you already have open. That assumption is wrong. LinkedIn Easy Apply does stay put; `amazon.jobs` hands you to `passport.amazon.jobs`, where a sign-in — usually verified by an emailed one-time code — stands in front of the *first* input field.

So knowledge is split into two layers, both keyed `<platform>/<route>`:

| layer | question it answers | example |
|---|---|---|
| selectors (`platforms`) | how do I locate this element | 3 candidates for `submit_button` |
| routes (`routes`) | how many steps, what does it need, which step needs a human | `Amazon/external_ats`: 8 steps, 1 `human_required` |

A route record carries `entry_signature` (how to recognise the route), `prerequisites`, `steps[]`, and the flywheel counters `runs` / `successes` / `blocked_at`. **`blocked_at` is more useful than the success rate**: the rate says a route is hard, `blocked_at` says *which gate* it dies at — which is where the next adapter gets written.

`route_guide(job_url)` returns all of this *before* the form is opened, so the human-required gates can be batched into the same question set as the profile answers rather than discovered halfway through.

### One browser at a time

"A single user" on this machine is really **at least three processes**: the MCP server your harness is talking to, the unattended loop, and you in a terminal. They share one `data/` and **one Chrome profile**, because the logged-in session is the one thing this project cannot rebuild for itself.

Two Chromes on one profile do not slow each other down — they overwrite each other's cookie database. So the profile has an exclusive lock and there is exactly one driver; a second one is refused with `browser_busy: true` naming the holder.

The state files are a different problem, and get a different mechanism:

| file | mechanism | accuracy |
|---|---|---|
| `guard_state.json` — daily cap, breaker, tokens | lock + read-modify-write | **exact**: 12 processes racing a cap of 5, exactly 5 get through |
| `memory.json` — the flywheel | lock + **merge**-on-write | no record lost; counters take the max |
| `application_log.json` — the ledger | same lock, merged by posting | no row lost or duplicated |

Deliberate choices:

- **`flock`, not pid files.** The kernel releases the lock however a process dies, `SIGKILL` included. So there is no stale-lock cleanup anywhere, and nothing has to answer the unanswerable question "is that pid still alive" — pids get recycled.
- **The lock is inheritable.** `flock` is held per open file description, so a child process's own `acquire()` is refused while its parent holds it. Every phase of an unattended pass is a child process, which is exactly why that loop once failed all three phases per pass. The parent now passes the descriptor down with `pass_fds`; the child recognises the same lock and proceeds without unlocking.
- **Merge, so not every writer has to be well-behaved.** A lock only stops processes that ask for it — not old code, not a throwaway script. Merge-on-write makes such writers survivable: the next save writes their records back, and `version` takes the max, so a schema rollback cannot outlive one save.
- **The cap's critical section is the browser lock.** "Decide → apply → record" sits inside one lock, so no separate reservation state is needed — reservations leak (a pass with 15 skips has no point at which to give one back).

`tests/test_concurrency.py` genuinely spawns processes: 8 writers on one file losing nothing, 12 processes racing a cap of 5, 4 processes spending one token, and a holder killed with `SIGKILL` releasing the lock immediately.

### Unattended runs

```bash
.venv/bin/python tools/cron_apply.py --dry-run    # rails + browser only, no applications
.venv/bin/python tools/cron_apply.py --ensure     # start the loop if it is not running
.venv/bin/python tools/cron_apply.py --status     # is it up? how much quota is left?
.venv/bin/python tools/cron_apply.py --stop
```

Run `--dry-run` first, and watch the first real submission. The loop drives the same Chrome profile you do.

### Privacy

The repository contains **no personal data of any kind**. No default name, email, phone, salary or location — `applyops-init` asks for all of it. `data/` is git-ignored in full, including the browser profile and its live session cookies.

The selectors and route seeds that *do* ship are public knowledge, not anyone's data: they give the first user a head start on their very first application, and are then corrected by real hit rates.

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

### 为什么是这个形态

这个项目的第一版自带大脑：agent 循环 + 三个可插拔 LLM provider + 一个 Web UI。问题不在代码质量，而在**位置**：

| 原设计的负担 | 把大脑搬出去之后 |
|---|---|
| 自己调模型 → 要 API Key | 没有了，大脑归 harness |
| 三个 LLM 适配器，加上模型退役漂移 | 整层删除 |
| agent 循环 → 无超时地空转 | MCP 是请求／响应：没有循环就没有空转 |
| 自己造的 Web UI | MCP client 就是前端 |

留下的只有两件**换任何一个 harness 都不会消失**的东西：浏览器层，和学习型记忆。`legacy/` 是退役的 v1，作为回滚点保留。

### 环境要求

- macOS 或 Linux，Python **≥ 3.12**
- [`uv`](https://docs.astral.sh/uv/)
- **Google Chrome**（由 Playwright 安装 —— 不是打包的 Chromium；导入 LinkedIn 登录态需要真实的 Chrome profile）
- 走 Easy Apply 需要一个 **LinkedIn 账号**

### 快速开始

```bash
git clone git@github.com:hanyuli0310/applyops-agent.git
cd applyops-agent

uv sync                                  # 建出 .venv/
uv run playwright install chrome

.venv/bin/applyops-init                  # 逐项问一遍填表要用的信息

.venv/bin/python tools/import_chrome_session.py --list     # 找出你已登录的 Chrome profile
.venv/bin/python tools/import_chrome_session.py --verify   # 确认登录态搬过来了

.venv/bin/applyops-mcp                   # stdio 起服务，在你的 harness 里注册它
```

然后先验证再信任：

```bash
.venv/bin/python tools/op.py ping '{}'            # 服务能通
.venv/bin/python tools/op.py setup_status '{}'    # 档案齐了没
.venv/bin/python -m pytest tests/ -q              # 22 项测试，约 6 秒
```

`tools/op.py <tool> '<json>'` 在 shell 里调用**恰好一个**工具，走的是真实 server 与真实护栏。手动驱动某一步，这是最快的路径。

### 首次运行会发生什么

没有需要手工配置的东西。第一次调 `setup_status()` 时，server 会报「档案不完整」，并返回一份 `questionnaire`；harness 一次把这些问题问完，答案再写回去。你也可以在终端里跑 `applyops-init` 走同一批字段 —— 两条路径由同一份字段定义生成，不可能不一致。

系统无法知道的信息留空。**留空代表「你还没说」，任何一层都禁止猜。** 薪资、签证状态、工作授权、法律声明，恰恰是「自信地答错」会造成实害的几类。

### 你的档案

你的个人信息存在**一个可以手改的 markdown 文件**里：`data/profile.md`。

```markdown
## 身份

- name: Jane Doe
- email: you@example.com
- phone:  <!-- What is your mobile number, with country code? -->
```

- 每行 `- 字段名: 值`。空值后面跟着一句 HTML 注释，代表「这里该填什么、系统会来问你」。
- 改完立即生效，不需要重跑任何命令。
- 全部 34 个字段，每个都附「为什么要问」，见 **`profile.example.md`**。

它和 `memory.json` 是两个文件，这是有意的：`memory.json` 装的是系统**学到**的东西，必须持续累积、**不该被人手改**；档案装的是只有**你**知道、而且你会想随时修正的东西。两个文件，两种生命周期。

两者都在 `data/` 下，而整个 `data/` 已被 git 忽略 —— 所以简历也可以直接丢进去，不会误提交。

### 五条不变式

代码就是围着这五条写的。破掉任何一条，都会得到一个**看起来能用**的系统。

1. **飞轮记录在工具内部，不在调用方。** `fill_field` 没有三参数版本。记录逻辑一旦活在调用方，换个 agent 循环就会静默饿死记忆，而 `memory.json` 看起来仍然健康。
2. **提交必须持有一次性确认令牌。** 没有 token 就提交不了 —— 这是唯一能真正拦住「误提交」的地方。
3. **档案空值只能问，不能猜。** 见上。
4. **`selectors_suggested` 在真实跑单后必须非零。** 它是唯一能区分「从没试过」和「试了全失败」的信号（两者的 `hit_rate` 都是 0）。恒为 0 就意味着记忆已经死了。
5. **人工关卡必须提前声明** —— 写在路由里，在开表单**之前**就拿到。

### 申请路由经验库：Easy Apply 才是简单的那条

选择器记忆回答「按钮长什么样」，同时默认了**表单就在你打开的这一页上**。这个假设是错的。LinkedIn Easy Apply 确实不出页面，但 `amazon.jobs` 会把你交给 `passport.amazon.jobs`，那里在**第一个输入框之前**就横着一道登录，而且往往要邮箱验证码。

所以知识分两层，key 都是 `<平台>/<路由>`：

| 层 | 回答的问题 | 例子 |
|---|---|---|
| 选择器（`platforms`） | 这个元素怎么定位 | `submit_button` 的 3 个候选 |
| 路由（`routes`） | 几步、前提是什么、哪一步必须人来做 | `Amazon/external_ats`：8 步，其中 1 步 `human_required` |

一条路由记录带 `entry_signature`（怎么认出这条路）、`prerequisites`（动手前得有什么）、`steps[]`，以及飞轮计数 `runs` / `successes` / `blocked_at`。**`blocked_at` 比成功率有用**：成功率只能说「这条路难」，`blocked_at` 说「死在哪道闸门」—— 下一个适配器就从这句话写起。

`route_guide(job_url)` 在**开表单之前**返回这些，所以人工关卡能和档案问题一起批量问掉，而不是填到一半才发现。

### 一次只开一个浏览器

这台机器上的「一个用户」其实**至少是三个进程**：harness 正在对话的 MCP server、无人值守的定时跑单、还有你在终端里手敲的那条。它们共用一个 `data/` 和**同一个 Chrome profile** —— 因为登录态是这个项目唯一自己造不出来的东西。

两个 Chrome 挤一个 profile 不是「慢一点」，是互相重写 cookie 数据库。所以 profile 有独占锁，同一时刻只有一个驱动；第二个会被拒绝，返回 `browser_busy: true` 并报出持有者是谁。

状态文件是另一类问题，用另一套机制：

| 文件 | 机制 | 精确度 |
|---|---|---|
| `guard_state.json` —— 每日上限 / 熔断 / 令牌 | 锁 + 读—改—写 | **精确**：12 个进程抢 5 个名额，结果就是 5 |
| `memory.json` —— 飞轮 | 锁 + **合并写** | 记录一条不丢；计数器取 max |
| `application_log.json` —— 台账 | 同一把锁，按 posting 合并 | 一条不丢、不重复 |

几个刻意的选择：

- **用 `flock`，不用 pid 文件。** 内核在进程以任何方式退出时释放锁（含 `SIGKILL`）。所以项目里**没有**陈旧锁清理，也没有任何地方需要回答「那个 pid 还活着吗」—— 这个问题没有安全答案，pid 会被回收。
- **锁可以交给子进程。** `flock` 按 open file description 持有，父进程持有时子进程自己 `acquire()` 会被内核拒绝。无人值守跑单的每个 phase 都是子进程 —— 这正是它曾经**每轮三个 phase 全部失败**的原因。现在父进程用 `pass_fds` 把描述符传下去，子进程认出「同一把锁」直接放行且不解锁。
- **合并，所以不要求所有写者都守规矩。** 锁只能拦住**主动请求**它的进程 —— 老版本代码、临时脚本拦不住。合并写让这类写者变得**可幸存**：下一次保存会把它不认识的记录替它写回去，`version` 取 max，所以 schema 回退活不过一次保存。
- **限额的临界区就是浏览器锁。** 「判定 → 投递 → 记录」整段放进同一把锁，因此不需要额外的名额预约状态 —— 预约会漏：一轮里 15 个 skip 都没有回收点。

`tests/test_concurrency.py` **真的** spawn 多进程：8 个进程并发写一个文件一条不丢、12 个进程抢 5 个名额恰好 5 个、4 个进程抢同一个令牌只有 1 个成功、持有者被 `SIGKILL` 后锁立刻可用。

### 无人值守

```bash
.venv/bin/python tools/cron_apply.py --dry-run    # 只跑护栏 + 浏览器，不投任何岗位
.venv/bin/python tools/cron_apply.py --ensure     # 没在跑就起起来
.venv/bin/python tools/cron_apply.py --status     # 在跑吗？还剩多少名额？
.venv/bin/python tools/cron_apply.py --stop
```

先跑 `--dry-run`，并且第一次真实投递建议盯屏 —— 它和你共用同一个 Chrome profile。

### 隐私

仓库里**不含任何人的个人信息**，一个默认名字、邮箱、电话、薪资、地址都没有 —— 全部由 `applyops-init` 问出来。`data/` 整个目录被 git 忽略，包括浏览器 profile 和里面活的会话 cookie。

随仓库走的先验知识（选择器、路由种子）是**公共知识**，不是某个人的数据：它们让第一个使用者在第一次投递时就有先手，之后再靠真实命中率自我修正。

### 许可

MIT，见 [LICENSE](LICENSE)。
