import { useCallback, useEffect, useState } from "react";
import { api, Application, Attempt, STATE_LABELS } from "../api";

interface Detail {
  application: Application;
  attempts: Attempt[];
  events: { at: string; kind: string }[];
}

export function ApplicationsPage({ onChanged }: { onChanged: () => void }) {
  const [apps, setApps] = useState<Application[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const reload = useCallback(() => {
    api.applications().then((r) => setApps(r.applications));
  }, []);

  useEffect(reload, [reload]);

  const open = async (id: string) => {
    try {
      setDetail(await api.applicationDetail(id));
    } catch (err) {
      setError(String((err as Error).message ?? err));
    }
  };

  const run = async (id: string) => {
    setError("");
    setMessage("");
    try {
      const res = await api.prepare(id);
      if (res.blocked) {
        setMessage(`被挡住了：${res.detail}`);
      } else {
        setMessage("表单已读取，等待你在「待我处理」页批准。");
      }
      reload();
      onChanged();
    } catch (err) {
      setError(String((err as Error).message ?? err));
    }
  };

  const submit = async (id: string, grantId: string) => {
    setError("");
    setMessage("");
    try {
      const res = await api.submit(id, grantId);
      setMessage(
        res.status === "verified"
          ? `已确认提交成功 —— ${res.detail}`
          : `结果：${res.status} —— ${res.detail}`
      );
      reload();
      onChanged();
    } catch (err) {
      setError(String((err as Error).message ?? err));
    }
  };

  return (
    <section>
      <h2>运行与记录</h2>
      <div className="card">
        {apps.length === 0 && <p>还没有任何申请记录。</p>}
        {apps.map((a) => (
          <div className="row" key={a.id} data-testid="application-row">
            <span className={`pill ${stateClass(a.state)}`}>{STATE_LABELS[a.state] ?? a.state}</span>
            <strong>{a.title || a.job_key}</strong>
            <span className="dim">{a.company}</span>
            <div className="actions">
              {a.state === "queued" && (
                <button data-testid={`run-${a.id}`} onClick={() => run(a.id)}>
                  准备申请
                </button>
              )}
              {a.state === "waiting_for_approval" && (
                <button
                  data-testid={`submit-${a.id}`}
                  onClick={async () => {
                    // 找这张申请最新一条待批准请求对应的 grant（由你批准时保存在本浏览器）。
                    const grantEntries = Object.keys(localStorage)
                      .filter((k) => k.startsWith("applyops-grant:"))
                      .map((k) => localStorage.getItem(k) ?? "")
                      .filter((v) => v !== "");
                    const grantId = grantEntries.length > 0 ? grantEntries[0] : "";
                    if (!grantId) {
                      setMessage("还没有批准记录 —— 先到「待我处理」页批准。");
                      return;
                    }
                    await submit(a.id, grantId);
                  }}
                >
                  提交
                </button>
              )}
              {["queued", "waiting_for_input", "waiting_for_approval"].includes(a.state) && (
                <button
                  onClick={async () => {
                    await api.cancel(a.id);
                    reload();
                    onChanged();
                  }}
                >
                  取消
                </button>
              )}
              <button onClick={() => open(a.id)}>详情</button>
            </div>
          </div>
        ))}
      </div>

      {detail && (
        <div className="card" data-testid="detail">
          <h3>
            {detail.application.title || detail.application.job_key} 的完整记录
          </h3>
          <h4>尝试（{detail.attempts.length}）</h4>
          {detail.attempts.map((a) => (
            <div className="row" key={a.id}>
              <span className="pill">{a.outcome || "进行中"}</span>
              <span>第 {a.ordinal} 次 · {a.started_at}</span>
              <span className="dim">{a.detail}</span>
            </div>
          ))}
          <h4>事件</h4>
          {detail.events.map((e, i) => (
            <div className="row" key={i}>
              <span className="dim">{e.at}</span>
              <span>{e.kind}</span>
            </div>
          ))}
          <div className="actions">
            <button
              onClick={() => {
                const blob = new Blob([JSON.stringify(detail, null, 2)], {
                  type: "application/json",
                });
                const link = document.createElement("a");
                link.href = URL.createObjectURL(blob);
                link.download = `applyops-${detail.application.job_key}.json`;
                link.click();
              }}
            >
              导出 JSON
            </button>
            <button onClick={() => setDetail(null)}>关闭</button>
          </div>
        </div>
      )}

      {message && <div className="banner ok" data-testid="run-message">{message}</div>}
      {error && <div className="banner error">{error}</div>}
    </section>
  );
}

function stateClass(state: string): string {
  if (state === "submitted_verified") return "ok";
  if (state.includes("unverified") || state.includes("waiting") || state === "failed") return "warn";
  return "";
}
