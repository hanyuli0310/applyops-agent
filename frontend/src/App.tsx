import { useCallback, useEffect, useState } from "react";
import { api, STATE_LABELS, Status } from "./api";
import { ApplicationsPage } from "./pages/Applications";
import { AttentionPage } from "./pages/Attention";
import { JobsPage } from "./pages/Jobs";
import { ProfilePage } from "./pages/Profile";

const TABS = [
  { key: "profile", label: "资料与简历" },
  { key: "jobs", label: "岗位与偏好" },
  { key: "attention", label: "待我处理" },
  { key: "runs", label: "运行与记录" },
] as const;

export type TabKey = (typeof TABS)[number]["key"];

export function App() {
  const [tab, setTab] = useState<TabKey>("profile");
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState("");

  const refreshStatus = useCallback(() => {
    api
      .status()
      .then(setStatus)
      .catch((err) => setError(String(err.message ?? err)));
  }, []);

  useEffect(() => {
    refreshStatus();
    const timer = setInterval(refreshStatus, 5000);
    return () => clearInterval(timer);
  }, [refreshStatus]);

  return (
    <div className="app">
      <header>
        <h1>ApplyOps 控制台</h1>
        {status && (
          <div className="statusline">
            <span className={status.profile_ready ? "ok" : "warn"}>
              档案{status.profile_ready ? "完整" : "未完整"}
            </span>
            <span>待处理 {status.pending_approvals}</span>
            <span>{status.browser_open ? "浏览器运行中" : "浏览器未启动"}</span>
            <span>v{status.version}</span>
          </div>
        )}
      </header>
      {error && <div className="banner error">{error}</div>}
      <nav>
        {TABS.map((t) => (
          <button
            key={t.key}
            className={tab === t.key ? "active" : ""}
            onClick={() => setTab(t.key)}
          >
            {t.label}
            {t.key === "attention" && status && status.pending_approvals > 0 && (
              <span className="badge">{status.pending_approvals}</span>
            )}
          </button>
        ))}
      </nav>
      <main>
        {tab === "profile" && <ProfilePage onChanged={refreshStatus} />}
        {tab === "jobs" && <JobsPage onChanged={refreshStatus} />}
        {tab === "attention" && <AttentionPage onChanged={refreshStatus} />}
        {tab === "runs" && <ApplicationsPage onChanged={refreshStatus} />}
      </main>
      <footer>
        <LiveStates status={status} />
      </footer>
    </div>
  );
}

function LiveStates({ status }: { status: Status | null }) {
  if (!status) return null;
  const entries = Object.entries(status.applications_by_state);
  if (entries.length === 0) return <span>队列为空</span>;
  return (
    <span>
      {entries
        .map(([state, n]) => `${STATE_LABELS[state] ?? state}: ${n}`)
        .join(" · ")}
    </span>
  );
}
