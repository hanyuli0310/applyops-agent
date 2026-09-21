import { useCallback, useEffect, useMemo, useState } from "react";
import { api, Application, PendingRequest, RunnerStatus, Status } from "../api";
import {
  ACTIVE_STATES,
  applicationProgress,
  displayCompany,
  displayTitle,
  filterApplications,
  formatRelativeTime,
  formatTime,
  nextAction,
  stateLabel,
} from "../dashboard";
import { AttentionPanel } from "../components/AttentionPanel";
import { ApplicationDetailDrawer } from "../components/ApplicationDetailDrawer";
import { MetricCards } from "../components/MetricCards";
import { StatusBadge } from "../components/StatusBadge";

interface DashboardProps {
  onChanged: () => void;
}

interface Snapshot {
  status: Status | null;
  applications: Application[];
  requests: PendingRequest[];
  runner: RunnerStatus | null;
}

const PRIORITY: Record<string, number> = {
  waiting_for_input: 1,
  waiting_for_approval: 2,
  submitted_unverified: 3,
  failed: 4,
  preparing: 5,
  submitting: 6,
  queued: 7,
};

export function Dashboard({ onChanged }: DashboardProps) {
  const [snapshot, setSnapshot] = useState<Snapshot>({ status: null, applications: [], requests: [], runner: null });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filters, setFilters] = useState({ state: "", company: "", platform: "" });
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState("");
  const [lastSyncedAt, setLastSyncedAt] = useState<Date | null>(null);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    const results = await Promise.allSettled([
      api.status(),
      api.applications(),
      api.pendingRequests(),
      api.runnerStatus(),
    ]);
    const [status, applications, requests, runner] = results;
    const errors = results.filter((result): result is PromiseRejectedResult => result.status === "rejected");
    setSnapshot((previous) => ({
      status: status.status === "fulfilled" ? status.value : previous.status,
      applications: applications.status === "fulfilled" ? applications.value.applications : previous.applications,
      requests: requests.status === "fulfilled" ? requests.value.requests : previous.requests,
      runner: runner.status === "fulfilled" ? runner.value : previous.runner,
    }));
    setLastSyncedAt(new Date());
    setStale(errors.length > 0);
    setError(errors.length > 0 ? "部分数据暂时无法同步，页面保留了上一次成功读取的内容。" : "");
    setLoading(false);
    setRefreshing(false);
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const selectedApplication = snapshot.applications.find((application) => application.id === selectedId);
  const selectedRequest = snapshot.requests.find((request) => request.application_id === selectedId);
  const filteredApplications = useMemo(
    () => filterApplications(snapshot.applications, filters),
    [snapshot.applications, filters]
  );
  const spotlight = useMemo(() => {
    return snapshot.applications
      .filter((application) => ACTIVE_STATES.has(application.state) || ["failed", "submitted_unverified"].includes(application.state))
      .sort((a, b) => (PRIORITY[a.state] ?? 99) - (PRIORITY[b.state] ?? 99))[0];
  }, [snapshot.applications]);

  const refreshAfterAction = async () => {
    await refresh();
    onChanged();
  };

  if (loading) {
    return <section className="dashboard-page"><div className="loading-state large">正在加载投递总览…</div></section>;
  }

  return (
    <section className="dashboard-page">
      <div className="page-heading dashboard-heading">
        <div>
          <span className="eyebrow">APPLICATION CONTROL CENTER</span>
          <h1>投递总览</h1>
          <p className="page-subtitle">所有岗位、所有待办、每一步证据，都在这里同步。</p>
        </div>
        <div className="sync-control">
          <span className={`sync-dot ${stale ? "stale" : ""}`} />
          <span>{stale ? "部分连接中断" : `最后同步 ${lastSyncedAt ? formatTime(lastSyncedAt.toISOString()) : "暂无"}`}</span>
          <button className="refresh-button" onClick={() => void refresh()} disabled={refreshing}>{refreshing ? "同步中…" : "刷新"}</button>
        </div>
      </div>

      {error && <div className="dashboard-alert"><span>!</span>{error}<button onClick={() => void refresh()}>重试</button></div>}
      <MetricCards applications={snapshot.applications} requests={snapshot.requests} status={snapshot.status} runner={snapshot.runner} />

      <div className="dashboard-grid">
        <div className="dashboard-main-column">
          <section className="spotlight panel">
            <div className="panel-heading"><div><span className="eyebrow">NOW IN FOCUS</span><h2>当前投递</h2></div>{spotlight && <button className="text-button" onClick={() => setSelectedId(spotlight.id)}>打开详情 →</button>}</div>
            {spotlight ? <Spotlight application={spotlight} onOpen={() => setSelectedId(spotlight.id)} /> : <div className="empty-state"><span className="empty-icon">✦</span><h3>队列现在是空的</h3><p>去“岗位与偏好”添加一个岗位，或启动本地 Demo ATS。</p></div>}
          </section>

          <section className="queue panel">
            <div className="panel-heading queue-heading"><div><span className="eyebrow">APPLICATION QUEUE</span><h2>全部岗位 <span className="heading-count">{filteredApplications.length}</span></h2></div><div className="filter-row"><select value={filters.state} onChange={(event) => setFilters({ ...filters, state: event.target.value })}><option value="">所有状态</option>{Object.keys(PRIORITY).map((state) => <option value={state} key={state}>{stateLabel(state)}</option>)}<option value="submitted_verified">已确认提交</option><option value="skipped">已跳过</option><option value="cancelled">已取消</option></select><input value={filters.company} onChange={(event) => setFilters({ ...filters, company: event.target.value })} placeholder="筛选公司" /></div></div>
            {filteredApplications.length === 0 ? <div className="empty-state compact"><p>没有符合当前筛选条件的岗位。</p></div> : <div className="application-table"><div className="application-table-head"><span>岗位</span><span>状态</span><span>进度</span><span>最近活动</span><span /></div>{filteredApplications.map((application) => <ApplicationRow key={application.id} application={application} onOpen={() => setSelectedId(application.id)} />)}</div>}
          </section>
        </div>
        <AttentionPanel applications={snapshot.applications} requests={snapshot.requests} onSelect={setSelectedId} />
      </div>

      {selectedApplication && <ApplicationDetailDrawer application={selectedApplication} request={selectedRequest} onClose={() => setSelectedId(null)} onChanged={refreshAfterAction} />}
    </section>
  );
}

function Spotlight({ application, onOpen }: { application: Application; onOpen: () => void }) {
  const progress = applicationProgress(application.state);
  return <button className="spotlight-card" onClick={onOpen}><div className="spotlight-card-top"><div><StatusBadge state={application.state} /><span className="spotlight-platform">{application.platform || application.route || "未知路线"}</span></div><span className="spotlight-time">{formatRelativeTime(application.updated_at || application.created_at)}</span></div><h3>{displayTitle(application)}</h3><p>{displayCompany(application)}{application.location ? ` · ${application.location}` : ""}</p><div className="progress-row"><div className="progress-track"><span style={{ width: `${progress}%` }} /></div><strong>{progress}%</strong></div><div className="spotlight-next"><span>{application.reason_text || `下一步：${nextAction(application)}`}</span><span className="arrow">→</span></div></button>;
}

function ApplicationRow({ application, onOpen }: { application: Application; onOpen: () => void }) {
  const progress = applicationProgress(application.state);
  return <button className="application-row" onClick={onOpen}><div className="application-cell identity-cell"><span className="company-mark">{(displayCompany(application)[0] || "?").toUpperCase()}</span><div><strong>{displayTitle(application)}</strong><span className="dim">{displayCompany(application)}{application.platform ? ` · ${application.platform}` : ""}</span></div></div><div><StatusBadge state={application.state} /></div><div className="mini-progress"><div className="progress-track"><span style={{ width: `${progress}%` }} /></div><span>{progress}%</span></div><span className="dim activity-time">{formatRelativeTime(application.updated_at || application.created_at)}</span><span className="row-arrow">→</span></button>;
}

