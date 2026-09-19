import { useCallback, useEffect, useState } from "react";
import { api, Application, Preferences, STATE_LABELS } from "../api";

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

  const reload = useCallback(() => {
    api.applications().then((r) => setJobs(r.applications));
  }, []);

  useEffect(reload, [reload]);
  useEffect(() => {
    api.preferences().then(setPrefs);
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

function splitCsv(value: string): string[] {
  return value
    .split(",")
    .map((v) => v.trim())
    .filter((v) => v !== "");
}
