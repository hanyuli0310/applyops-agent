# ApplyOps Profile — 字段说明与示例

这是 `data/profile.md` 的带注释示例。**真正的档案不在这个文件里。**

两种方式生成你的 `data/profile.md`：

1. 跑 `applyops-init`（推荐）—— 逐项问你，只问空着的字段；
2. 把这个文件复制成 `data/profile.md` 再手填。

格式很简单：`## 分组` 标题下面写 `- 字段名: 值`。空值表示「还不知道」，
系统遇到空值会去问你，而**不会自己猜**。

标 **必填** 的字段缺失时投递会停在半路，建议先填齐。

> `data/` 整个目录已在 `.gitignore` 里，所以简历也可以直接放进
> `data/`，不会误提交。

---

## 身份

### `name` — 必填

- 问题：What is your full legal name?
- 提示：Exactly as it appears on your ID, not a nickname.
- 示例：`Jane Doe`

### `email` — 必填

- 问题：Which email address should applications use?
- 提示：Recruiter replies land here, so use one you actually read.
- 示例：`you@example.com`

### `phone` — 必填

- 问题：What is your mobile number, with country code?
- 示例：`+1 555 010 4477`

### `location` — 必填

- 问题：Where do you currently live?
- 提示：City and country at minimum; forms often split this into fields.
- 示例：`Austin, TX, USA`

### `linkedin_url` — 选填

- 问题：Your LinkedIn profile URL?
- 示例：`https://www.linkedin.com/in/your-handle`

### `github_url` — 选填

- 问题：Your GitHub profile URL?
- 为什么要问：Engineering forms ask for this more often than you would expect.

### `website_url` — 选填

- 问题：A portfolio or personal site?

## 简历

### `resume_path` — 必填

- 问题：Absolute path to the resume PDF you want to submit?
- 取值：文件的绝对路径
- 提示：A copy inside data/ works well -- that whole directory is git-ignored, so the file cannot be committed by accident.
- 为什么要问：Every application uploads this file. If the path is wrong the submit step fails after the form is already filled.
- 示例：`/Users/you/data/resume.pdf`

### `cover_letter_path` — 选填

- 问题：A default cover letter PDF, if you keep one?
- 取值：文件的绝对路径

## 工作授权

### `work_authorization` — 必填

- 问题：What is your legal right to work where you are applying?
- 取值：US Citizen / US Permanent Resident (Green Card) / H-1B / F-1 (student, CPT / OPT) / F-1 OPT / F-1 CPT / TN / Requires sponsorship / Other
- 为什么要问：Nearly every US posting asks this, and it is a question the system must never answer on your behalf by guessing.

### `requires_sponsorship` — 必填

- 问题：Will you need visa sponsorship now or in the future?
- 取值：yes / no
- 为什么要问：Note the wording: most forms mean 'ever', not 'for this job'.

### `notice_period` — 选填

- 问题：What notice period does your current job require?
- 示例：`2 weeks`

### `earliest_start_date` — 选填

- 问题：What is the earliest date you could start?
- 示例：`2026-11-01`

## 经历

### `years_experience` — 必填

- 问题：How many years of full-time professional experience do you have?
- 取值：纯数字
- 提示：A whole number of years. Entry level is 0.
- 示例：`3`

### `current_title` — 必填

- 问题：What is your current or most recent job title?
- 示例：`Software Engineer`

### `current_company` — 必填

- 问题：Who is your current or most recent employer?

### `highest_degree` — 选填

- 问题：What is the highest degree you have completed, or are currently pursuing?
- 取值：High School / Associate / Bachelor's / Master's / PhD / Other
- 提示：If you are mid-degree, name that degree -- it is what screeners filter on -- and record the expected date in graduation_date.

### `school` — 选填

- 问题：Which school awarded that degree?

### `major` — 选填

- 问题：What did you study?

### `graduation_date` — 选填

- 问题：When did -- or will -- you graduate?
- 提示：YYYY-MM. Near-universal on student and new-grad applications; they use it to slot you into a cohort.
- 示例：`2027-03`

## 薪酬

### `expected_salary` — 必填

- 问题：What annual salary are you targeting? Digits only.
- 取值：纯数字
- 提示：A single number, or 'open' if you have no figure in mind. The field will be rendered into whatever format the form asks for (per year, per hour where obvious).
- 示例：`150000`

### `salary_currency` — 必填

- 问题：Which currency is that figure in?
- 取值：USD / CNY / EUR / GBP / CAD / AUD / SGD

### `current_salary` — 选填

- 问题：Your current annual salary, digits only?
- 取值：纯数字
- 为什么要问：Frequently asked, frequently refused. Leaving it blank is a valid answer -- the system will ask rather than invent one.

### `salary_negotiable` — 选填

- 问题：Is your expected salary negotiable?
- 取值：yes / no

## 求职偏好

### `willing_locations` — 必填

- 问题：Which locations would you accept? Comma separated.
- 提示：Include Remote if that is on the table.
- 示例：`Austin, TX, New York, NY, Remote`

### `work_mode` — 选填

- 问题：Which work arrangement do you prefer?
- 取值：remote / hybrid / onsite / any

### `willing_to_relocate` — 选填

- 问题：Are you open to relocating?
- 取值：yes / no

### `target_titles` — 选填

- 问题：Which job titles should the system search for? Comma separated.
- 提示：Used when discovering postings rather than applying to a link you were handed.
- 示例：`Software Engineer, Backend Engineer`

## 常见法律问题

_这些问题美国申请表常问。答错比答慢更糟，所以宁可留空去问。_

### `felony_conviction` — 选填

- 问题：Have you ever been convicted of a felony?
- 取值：yes / no
- 为什么要问：US forms ask this and it must be answered truthfully or not at all. Never let a system guess here.

### `non_compete_agreement` — 选填

- 问题：Are you currently bound by a non-compete agreement?
- 取值：yes / no

### `background_check_ok` — 选填

- 问题：Do you consent to a background check?
- 取值：yes / no

## 自愿人口统计

_美国 EEO 自愿统计问题，法律上完全可选，全部可以回答 「Prefer not to say」或者整组跳过。_

### `gender` — 选填

- 问题：Gender (voluntary)?
- 取值：Male / Female / Non-binary / Prefer not to say

### `race_ethnicity` — 选填

- 问题：Race / ethnicity (voluntary)?
- 取值：Asian / Black or African American / Hispanic or Latino / White / Two or more races / Prefer not to say

### `veteran_status` — 选填

- 问题：Veteran status (voluntary)?
- 取值：I am not a protected veteran / I am a protected veteran / Prefer not to say

### `disability_status` — 选填

- 问题：Disability status (voluntary)?
- 取值：Yes, I have a disability / No, I do not have a disability / Prefer not to say

---

## 自定义字段

想额外记点什么（比如 `security_clearance: TS/SCI`），直接在
`data/profile.md` 里加一行 `- 字段名: 值` 就行。系统会原样保存，
但不会主动拿来填表 —— 除非名字正好对上表单问的问题。
