import { useCallback, useEffect, useState } from "react";
import { api, Application, CompanyPolicy, Preferences, STATE_LABELS } from "../api";

export function JobsPage({ onChanged }: { onChanged: () => void }) {
  const [jobs, setJobs] = useState<Application[]>([]);
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [company, setCompany] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [prefs, setPrefs] = useState<Preferences>({
    target_titles: [],
    locations: [],
    include_keywords: [],
    exclude_keywords: [],
    exclude_companies: [],
  });
  const [preview, setPreview] = useState<{ keep: boolean; reasons: string[] } | null>(null);
  const [companyPolicy, setCompanyPolicy] = useState<CompanyPolicy>({
    default_policy: "auto",
    review_companies: [],
    never_companies: [],
  });
  const [reviewInput, setReviewInput] = useState("");
  const [neverInput, setNeverInput] = useState("");

  const reload = useCallback(() => {
    api.applications().then((r) => setJobs(r.applications));
  }, []);

  useEffect(reload, [reload]);
  useEffect(() => {
    api.preferences().then(setPrefs);
    api.companyPolicy().then(setCompanyPolicy);
  }, []);

  const add = async (body: { job_url: string; title?: string; company?: string }) => {
    setError("");
    setMessage("");
    try {
      const res = await api.enqueue(body);
      setMessage(`已入队：${res.application.title || res.application.job_key}`);
      setUrl("");
      setTitle("");
      setCompany("");
      reload();
      onChanged();
    } catch (err) {
      setError(String((err as Error).message ?? err));
    }
  };

  const savePrefs = async () => {
    setError("");
    try {
      const saved = await api.savePreferences(prefs);
      setPrefs(saved);
      setMessage("偏好已保存。新入队的岗位会按这些规则判断，并给出理由。");
    } catch (err) {
      setError(String((err as Error).message ?? err));
    }
  };

  const saveCompanyPolicy = async () => {
    setError("");
    try {
      const saved = await api.saveCompanyPolicy(companyPolicy);
      setCompanyPolicy(saved);
      setMessage("公司投递策略已保存：未列出的公司自动投递。\n");
      onChanged();
    } catch (err) {
      setError(String((err as Error).message ?? err));
    }
  };

  const addCompany = (kind: "review_companies" | "never_companies") => {
    const value = (kind === "review_companies" ? reviewInput : neverInput).trim();
    if (!value) return;
    setCompanyPolicy({
      ...companyPolicy,
      [kind]: [...companyPolicy[kind], value],
    });
    if (kind === "review_companies") setReviewInput("");
    else setNeverInput("");
  };

  const removeCompany = (kind: "review_companies" | "never_companies", index: number) => {
    setCompanyPolicy({
      ...companyPolicy,
      [kind]: companyPolicy[kind].filter((_value, current) => current !== index),
    });
  };

  const runPreview = async () => {
    setError("");
    try {
      const decision = await api.previewPreference({ title, company, location: url });
      setPreview(decision);
    } catch (err) {
      setError(String((err as Error).message ?? err));
    }
  };

  const csv = (values: string[]) => values.join(", ");

  const startDemo = async () => {
    setError("");
    try {
      const res = await api.startDemo();
      setMessage(`演示岗位已入队（本地 Demo ATS：${res.demo_url}）。整条流程不出本机。`);
      reload();
      onChanged();
    } catch (err) {
      setError(String((err as Error).message ?? err));
    }
  };

  const active = jobs.filter((j) =>
    ["queued", "preparing", "waiting_for_input", "waiting_for_approval"].includes(j.state)
  );

  return (
    <section>
      <h2>岗位与偏好</h2>
      <div className="card">
        <h3>添加岗位</h3>
        <label>
          岗位链接
          <input
            data-testid="job-url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://…（或用下面的演示按钮）"
          />
        </label>
        <label>
          职位名称（可选）
          <input data-testid="job-title" value={title} onChange={(e) => setTitle(e.target.value)} />
        </label>
        <label>
          公司（可选）
          <input data-testid="job-company" value={company} onChange={(e) => setCompany(e.target.value)} />
        </label>
        <div className="actions">
          <button data-testid="job-add" onClick={() => add({ job_url: url, title, company })} disabled={!url.trim()}>
            入队
          </button>
          <button data-testid="demo-start" onClick={startDemo}>
            一键添加演示岗位（本地）
          </button>
          <button
            data-testid="preview-rule"
            onClick={runPreview}
            disabled={!title.trim()}
          >
            为什么保留/过滤？（预览）
          </button>
        </div>
        {preview && (
          <div className={preview.keep ? "banner ok" : "banner error"} data-testid="preview-result">
            <strong>{preview.keep ? "会被保留" : "会被过滤"}</strong>
            <ul>
              {preview.reasons.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          </div>
        )}
      </div>

      <div className="card">
        <h3>筛选偏好（决定什么岗位会被留下，以及为什么）</h3>
        <p className="hint">
          标题按「整词」匹配：填 <code>backend engineer</code> 会匹配 “Senior Backend Engineer”，
          但不会匹配 “Frontend Engineer”，也不会因为 <code>intern</code> 而误伤 “internal tools”。
        </p>
        <label>
          目标职位（逗号分隔）
          <input
            data-testid="pref-titles"
            value={csv(prefs.target_titles)}
            onChange={(e) => setPrefs({ ...prefs, target_titles: splitCsv(e.target.value) })}
          />
        </label>
        <label>
          目标地点（逗号分隔）
          <input
            data-testid="pref-locations"
            value={csv(prefs.locations)}
            onChange={(e) => setPrefs({ ...prefs, locations: splitCsv(e.target.value) })}
          />
        </label>
        <label>
          必须包含关键词（逗号分隔）
          <input
            data-testid="pref-include"
            value={csv(prefs.include_keywords)}
            onChange={(e) => setPrefs({ ...prefs, include_keywords: splitCsv(e.target.value) })}
          />
        </label>
        <label>
          排除关键词（逗号分隔）
          <input
            data-testid="pref-exclude"
            value={csv(prefs.exclude_keywords)}
            onChange={(e) => setPrefs({ ...prefs, exclude_keywords: splitCsv(e.target.value) })}
          />
        </label>
        <label>
          排除公司（逗号分隔）
          <input
            data-testid="pref-companies"
            value={csv(prefs.exclude_companies)}
            onChange={(e) => setPrefs({ ...prefs, exclude_companies: splitCsv(e.target.value) })}
          />
        </label>
        <div className="actions">
          <button data-testid="pref-save" onClick={savePrefs}>
            保存偏好
          </button>
        </div>
      </div>

      <div className="card" data-testid="company-policy">
        <h3>自动投递公司策略</h3>
        <p className="hint">
          未列出的公司自动投递；人工确认名单会自动准备并出现在「待我处理」；永不投递名单会直接跳过。
        </p>
        <label>
          默认策略
          <select value={companyPolicy.default_policy} disabled>
            <option value="auto">未列出的公司自动投递</option>
          </select>
        </label>
        <CompanyList
          title="需要人工确认"
          values={companyPolicy.review_companies}
          input={reviewInput}
          onInput={setReviewInput}
          onAdd={() => addCompany("review_companies")}
          onRemove={(index) => removeCompany("review_companies", index)}
          testId="review-companies"
        />
        <CompanyList
          title="永不投递"
          values={companyPolicy.never_companies}
          input={neverInput}
          onInput={setNeverInput}
          onAdd={() => addCompany("never_companies")}
          onRemove={(index) => removeCompany("never_companies", index)}
          testId="never-companies"
        />
        <div className="actions">
          <button data-testid="company-policy-save" onClick={saveCompanyPolicy}>
            保存公司策略
          </button>
        </div>
      </div>

      <div className="card">
        <h3>队列中（{active.length}）</h3>
        {active.length === 0 && <p>队列为空。添加一个岗位，或点上面的演示按钮。</p>}
        {active.map((j) => (
          <div className="row" key={j.id} data-testid="queue-row">
            <span className="pill">{STATE_LABELS[j.state] ?? j.state}</span>
            <strong>{j.title || j.job_key}</strong>
            <span className="dim">{j.company}</span>
          </div>
        ))}
      </div>

      {message && <div className="banner ok" data-testid="jobs-message">{message}</div>}
      {error && <div className="banner error">{error}</div>}
    </section>
  );
}

function CompanyList({
  title,
  values,
  input,
  onInput,
  onAdd,
  onRemove,
  testId,
}: {
  title: string;
  values: string[];
  input: string;
  onInput: (value: string) => void;
  onAdd: () => void;
  onRemove: (index: number) => void;
  testId: string;
}) {
  return (
    <div className="company-list" data-testid={testId}>
      <h4>{title}</h4>
      <div className="chips">
        {values.map((value, index) => (
          <span className="pill" key={`${value}-${index}`}>
            {value}
            <button
              type="button"
              aria-label={`删除 ${value}`}
              onClick={() => onRemove(index)}
            >
              ×
            </button>
          </span>
        ))}
      </div>
      <div className="actions">
        <input
          value={input}
          placeholder="输入公司名"
          onChange={(event) => onInput(event.target.value)}
          onKeyDown={(event) => event.key === "Enter" && onAdd()}
        />
        <button type="button" onClick={onAdd}>
          + 添加
        </button>
      </div>
    </div>
  );
}

function splitCsv(value: string): string[] {
  return value
    .split(",")
    .map((v) => v.trim())
    .filter((v) => v !== "");
}
