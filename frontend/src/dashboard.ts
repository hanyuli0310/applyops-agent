import { Application, PendingRequest, STATE_LABELS } from "./api";

export const ACTIVE_STATES = new Set([
  "queued",
  "preparing",
  "waiting_for_input",
  "waiting_for_approval",
  "submitting",
]);

export const ATTENTION_STATES = new Set([
  "waiting_for_input",
  "waiting_for_approval",
  "submitted_unverified",
  "failed",
]);

const PROGRESS: Record<string, number> = {
  queued: 12,
  preparing: 32,
  waiting_for_input: 42,
  waiting_for_approval: 68,
  submitting: 86,
  submitted_verified: 100,
  submitted_unverified: 100,
  failed: 100,
  cancelled: 100,
  skipped: 100,
  legacy_imported: 100,
};

export function applicationProgress(state: string): number {
  return PROGRESS[state] ?? 18;
}

export function stateLabel(state: string): string {
  return STATE_LABELS[state] ?? state.replaceAll("_", " ");
}

export function stateClass(state: string): string {
  if (state === "submitted_verified") return "ok";
  if (state === "failed" || state === "submitted_unverified") return "danger";
  if (ATTENTION_STATES.has(state)) return "warn";
  if (ACTIVE_STATES.has(state)) return "info";
  return "neutral";
}

export function displayTitle(application: Application): string {
  return application.title || application.job_key || "未命名岗位";
}

export function displayCompany(application: Application): string {
  return application.company || "未注明公司";
}

export function formatTime(value?: string): string {
  if (!value) return "暂无时间";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatRelativeTime(value?: string): string {
  if (!value) return "暂无活动";
  const date = new Date(value).getTime();
  if (Number.isNaN(date)) return value;
  const minutes = Math.round((Date.now() - date) / 60000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.round(hours / 24)} 天前`;
}

export function countByState(applications: Application[]): Record<string, number> {
  return applications.reduce<Record<string, number>>((counts, application) => {
    counts[application.state] = (counts[application.state] ?? 0) + 1;
    return counts;
  }, {});
}

export function attentionCount(
  applications: Application[],
  requests: PendingRequest[]
): number {
  const requestIds = new Set(requests.map((request) => request.application_id));
  const applicationAttention = applications.filter(
    (application) => ATTENTION_STATES.has(application.state) && !requestIds.has(application.id)
  ).length;
  return requests.length + applicationAttention;
}

export function nextAction(application: Application): string {
  const actions = application.available_actions ?? [];
  if (actions.includes("answer")) return "补充信息";
  if (actions.includes("approve")) return "查看审批";
  if (actions.includes("reconcile")) return "核实结果";
  if (actions.includes("prepare")) return "开始准备";
  if (actions.includes("retry")) return "重新准备";
  if (application.state === "submitted_verified") return "查看记录";
  return "查看详情";
}

export function filterApplications(
  applications: Application[],
  filters: { state: string; company: string; platform: string }
): Application[] {
  const company = filters.company.trim().toLowerCase();
  const platform = filters.platform.trim().toLowerCase();
  return applications.filter((application) => {
    const stateMatches = !filters.state || application.state === filters.state;
    const companyMatches = !company || application.company.toLowerCase().includes(company);
    const platformMatches = !platform || application.platform.toLowerCase() === platform;
    return stateMatches && companyMatches && platformMatches;
  });
}

