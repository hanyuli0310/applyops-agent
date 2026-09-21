import { Application, PendingRequest, RunnerStatus, Status } from "../api";
import { attentionCount, ATTENTION_STATES, ACTIVE_STATES } from "../dashboard";

interface MetricCardsProps {
  applications: Application[];
  requests: PendingRequest[];
  status: Status | null;
  runner: RunnerStatus | null;
}

export function MetricCards({ applications, requests, status, runner }: MetricCardsProps) {
  const active = applications.filter((application) => ACTIVE_STATES.has(application.state)).length;
  const inProgress = applications.filter((application) =>
    ["preparing", "submitting"].includes(application.state)
  ).length;
  const completed = applications.filter((application) => application.state === "submitted_verified").length;
  const attention = attentionCount(applications, requests);

  const cards = [
    { label: "待处理岗位", value: active, note: `${applications.length} 个岗位在队列`, tone: "blue" },
    { label: "正在投递", value: inProgress, note: status?.browser_open ? "浏览器正在运行" : "浏览器待启动", tone: "violet" },
    { label: "需要你处理", value: attention, note: requests.length ? `${requests.length} 个审批或回答` : "暂无待办", tone: attention ? "amber" : "green" },
    { label: "已确认完成", value: completed, note: runner?.paused ? "后台运行已暂停" : "持续同步中", tone: "green" },
  ];

  return (
    <div className="metric-grid" aria-label="投递统计">
      {cards.map((card) => (
        <div className={`metric-card ${card.tone}`} key={card.label}>
          <span className="metric-label">{card.label}</span>
          <strong className="metric-value">{card.value}</strong>
          <span className="metric-note">{card.note}</span>
        </div>
      ))}
    </div>
  );
}

