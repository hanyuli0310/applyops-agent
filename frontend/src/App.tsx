import { useCallback, useEffect, useState } from "react";
import { api, STATE_LABELS, Status } from "./api";
import { ApplicationsPage } from "./pages/Applications";
import { AttentionPage } from "./pages/Attention";
import { Dashboard } from "./pages/Dashboard";
import { JobsPage } from "./pages/Jobs";
import { ProfilePage } from "./pages/Profile";

const TABS = [
  { key: "dashboard", label: "投递总览", icon: "⌂" },
  { key: "profile", label: "资料与简历" },
  { key: "jobs", label: "岗位与偏好" },
  { key: "attention", label: "待我处理" },
  { key: "runs", label: "运行与记录" },
] as const;

export type TabKey = (typeof TABS)[number]["key"];

export function App() {
  const [tab, setTab] = useState<TabKey>("dashboard");
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
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup"><span className="brand-mark">A</span><div><strong>ApplyOps</strong><span>投递控制台</span></div></div>
        <nav className="side-nav" aria-label="主导航">
          {TABS.map((t) => (
            <button key={t.key} className={tab === t.key ? "active" : ""} onClick={() => setTab(t.key)}>
              <span className="nav-icon">{"icon" in t && t.icon ? t.icon : t.key === "profile" ? "◉" : t.key === "jobs" ? "▦" : t.key === "attention" ? "!" : "↗"}</span>
              <span>{t.label}</span>
              {t.key === "attention" && status && status.pending_approvals > 0 && <span className="nav-count">{status.pending_approvals}</span>}
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          <div className={`connection-dot ${status?.browser_open ? "online" : ""}`} />
          <div><strong>{status?.browser_open ? "浏览器已连接" : "浏览器未启动"}</strong><span>ApplyOps v{status?.version ?? "0.2"}</span></div>
        </div>
      </aside>
      <div className="app-content">
        <header className="topbar">
          <div className="mobile-brand"><span className="brand-mark">A</span><strong>ApplyOps</strong></div>
          {status && <div className="statusline"><span className={status.profile_ready ? "ok" : "warn"}>资料{status.profile_ready ? "完整" : "待补充"}</span><span>待审批 {status.pending_approvals}</span><span>同步中</span></div>}
        </header>
        {error && <div className="global-error banner error">{error}</div>}
        <main className="content-main">
          {tab === "dashboard" && <Dashboard onChanged={refreshStatus} />}
          {tab === "profile" && <ProfilePage onChanged={refreshStatus} />}
          {tab === "jobs" && <JobsPage onChanged={refreshStatus} />}
          {tab === "attention" && <AttentionPage onChanged={refreshStatus} />}
          {tab === "runs" && <ApplicationsPage onChanged={refreshStatus} />}
        </main>
        <footer><LiveStates status={status} /></footer>
      </div>
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
