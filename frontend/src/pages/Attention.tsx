import { useCallback, useEffect, useState } from "react";
import { api, Application, PendingRequest, STATE_LABELS } from "../api";

export function AttentionPage({ onChanged }: { onChanged: () => void }) {
  const [requests, setRequests] = useState<PendingRequest[]>([]);
  const [inputNeeded, setInputNeeded] = useState<Application[]>([]);
  const [unverified, setUnverified] = useState<Application[]>([]);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const reload = useCallback(() => {
    api.pendingRequests().then((r) => setRequests(r.requests));
    api.applications("waiting_for_input").then((r) => setInputNeeded(r.applications));
    api.applications("submitted_unverified").then((r) => setUnverified(r.applications));
  }, []);

  useEffect(reload, [reload]);

  const approve = async (requestId: string) => {
    setError("");
    try {
      const res = await api.approve(requestId);
      // The grant belongs to the human's decision, so it travels through the
      // human's own browser: scoped to this job, single-use, expiring anyway.
      localStorage.setItem(`applyops-grant:${requestId}`, res.grant_id);
      setMessage("已批准。回到「运行与记录」页点击提交。");
      reload();
      onChanged();
    } catch (err) {
      setError(String((err as Error).message ?? err));
    }
  };

  const reject = async (requestId: string) => {
    await api.reject(requestId);
    reload();
    onChanged();
  };

  return (
    <section>
      <h2>待我处理</h2>

      <div className="card">
        <h3>等待你批准的提交（{requests.length}）</h3>
        {requests.length === 0 && <p>没有待批准的提交。</p>}
        {requests.map((r) => (
          <div className="card request" key={r.request_id} data-testid="approval-request">
            <pre className="summary">{r.summary}</pre>
            <p className="hint">批准 = 同意把上面列出的内容原样发给对方，无法撤回。</p>
            <div className="actions">
              <button
                className="primary"
                data-testid="approve-button"
                onClick={() => approve(r.request_id)}
              >
                批准这次提交
              </button>
              <button data-testid="reject-button" onClick={() => reject(r.request_id)}>
                拒绝
              </button>
            </div>
          </div>
        ))}
      </div>

      <div className="card">
        <h3>等你补充的岗位（{inputNeeded.length}）</h3>
        {inputNeeded.length === 0 && <p>没有缺信息的岗位。</p>}
        {inputNeeded.map((a) => (
          <div className="row" key={a.id}>
            <span className="pill warn">{STATE_LABELS[a.state]}</span>
            <strong>{a.title || a.job_key}</strong>
            <span className="dim">
              缺资料或简历未配置 —— 去「资料与简历」补齐后它会自动回到准备流程
            </span>
          </div>
        ))}
      </div>

      <div className="card">
        <h3>已发送但未确认（{unverified.length}）</h3>
        {unverified.length === 0 && <p>没有未知结果的提交。</p>}
        {unverified.map((a) => (
          <div className="row" key={a.id}>
            <span className="pill warn">{STATE_LABELS[a.state]}</span>
            <strong>{a.title || a.job_key}</strong>
            <button
              data-testid={`reconcile-${a.id}`}
              onClick={async () => {
                try {
                  const res = await api.reconcile(a.id);
                  setMessage(`核实结果：${res.status} —— ${res.detail}`);
                  reload();
                  onChanged();
                } catch (err) {
                  setError(String((err as Error).message ?? err));
                }
              }}
            >
              重新核实（不会再次提交）
            </button>
          </div>
        ))}
      </div>

      {message && <div className="banner ok">{message}</div>}
      {error && <div className="banner error">{error}</div>}
    </section>
  );
}
