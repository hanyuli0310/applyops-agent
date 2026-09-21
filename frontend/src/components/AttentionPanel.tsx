import { Application, PendingRequest } from "../api";
import { ATTENTION_STATES, displayCompany, displayTitle, nextAction } from "../dashboard";
import { StatusBadge } from "./StatusBadge";

interface AttentionPanelProps {
  applications: Application[];
  requests: PendingRequest[];
  onSelect: (applicationId: string) => void;
}

export function AttentionPanel({ applications, requests, onSelect }: AttentionPanelProps) {
  const byId = new Map(applications.map((application) => [application.id, application]));
  const items = [
    ...requests.map((request) => ({
      id: request.application_id,
      state: "waiting_for_approval",
      title: byId.get(request.application_id)?.title || request.job_key,
      company: request.company || byId.get(request.application_id)?.company || "",
      reason: request.reason,
      action: "查看审批",
    })),
    ...applications
      .filter((application) => ATTENTION_STATES.has(application.state))
      .filter((application) => !requests.some((request) => request.application_id === application.id))
      .map((application) => ({
        id: application.id,
        state: application.state,
        title: displayTitle(application),
        company: displayCompany(application),
        reason: application.reason_text || "需要查看详情",
        action: nextAction(application),
      })),
  ];

  return (
    <aside className="attention-panel panel" aria-label="待我处理">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">ACTION REQUIRED</span>
          <h2>待我处理</h2>
        </div>
        <span className="count-chip">{items.length}</span>
      </div>
      {items.length === 0 ? (
        <div className="empty-state compact">
          <span className="empty-icon">✓</span>
          <p>目前没有需要你处理的事项。</p>
        </div>
      ) : (
        <div className="attention-list">
          {items.slice(0, 6).map((item) => (
            <button className="attention-item" key={`${item.id}-${item.state}`} onClick={() => onSelect(item.id)}>
              <span className="attention-item-top">
                <StatusBadge state={item.state} />
                <span className="attention-action">{item.action} →</span>
              </span>
              <strong>{item.title}</strong>
              <span className="dim">{item.company}</span>
              <span className="attention-reason">{item.reason}</span>
            </button>
          ))}
          {items.length > 6 && <p className="dim attention-more">还有 {items.length - 6} 项待处理</p>}
        </div>
      )}
    </aside>
  );
}

