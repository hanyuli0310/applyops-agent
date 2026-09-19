import { useCallback, useEffect, useState } from "react";
import { api, Application, STATE_LABELS } from "../api";

export function JobsPage({ onChanged }: { onChanged: () => void }) {
  const [jobs, setJobs] = useState<Application[]>([]);
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [company, setCompany] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const reload = useCallback(() => {
    api.applications().then((r) => setJobs(r.applications));
  }, []);

  useEffect(reload, [reload]);

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

      {message && <div className="banner ok">{message}</div>}
      {error && <div className="banner error">{error}</div>}
    </section>
  );
}
