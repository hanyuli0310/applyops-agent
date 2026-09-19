const BASE = "/api";

/**
 * The session token the server baked into this page.
 *
 * State-changing requests carry it, which is what distinguishes a click in this
 * console from a form post by some other page the user happens to have open.
 */
function sessionToken(): string {
  return (window as unknown as { __APPLYOPS_TOKEN__?: string }).__APPLYOPS_TOKEN__ ?? "";
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const method = (options?.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (method !== "GET" && method !== "HEAD") {
    headers["X-ApplyOps-Token"] = sessionToken();
  }
  const res = await fetch(`${BASE}${path}`, {
    ...options,
    headers: { ...headers, ...(options?.headers as Record<string, string> | undefined) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      /* keep statusText */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export interface Status {
  version: string;
  python: string;
  os: string;
  profile_ready: boolean;
  missing_required: string[];
  browser_open: boolean;
  applications_by_state: Record<string, number>;
  pending_approvals: number;
  guardrails: Record<string, unknown>;
}

export interface Application {
  id: string;
  job_key: string;
  job_url: string;
  route: string;
  platform: string;
  state: string;
  title: string;
  company: string;
  created_at: string;
  display_state?: string;
  reason_code?: string;
  reason_text?: string;
  available_actions?: string[];
}

export interface Attempt {
  id: string;
  ordinal: number;
  outcome: string;
  detail: string;
  started_at: string;
  ended_at: string;
}

export interface PendingRequest {
  request_id: string;
  job_key: string;
  job_url: string;
  application_id: string;
  created_at: string;
  company: string;
  reason: string;
  reason_code: string;
  available_actions: string[];
  summary: string;
}

export interface FillOutcome {
  label: string;
  ref: string;
  source: string;
  verification: string;
  detail: string;
}

export interface FillReport {
  ready: boolean;
  filled: FillOutcome[];
  unfilled_required: string[];
  unfilled_optional: string[];
  unreadable: string[];
  mismatched: FillOutcome[];
  resume: FillOutcome | null;
  problems: string[];
}

export interface RunnerStatus {
  paused: boolean;
  stopped: boolean;
  policy_usable: boolean;
  policy: { enabled: boolean; max_applications: number; allowed_platforms: string[] };
  company_policy: CompanyPolicy;
  review_waiting: { application_id: string; title: string; company: string; reason: string }[];
  waiting_for_input: {
    application_id: string;
    title: string;
    missing: string[];
    reason_code?: string;
    reason_text?: string;
    available_actions?: string[];
  }[];
}

export interface CompanyPolicy {
  default_policy: "auto";
  review_companies: string[];
  never_companies: string[];
  updated_at?: string;
}

export interface Preferences {
  target_titles: string[];
  locations: string[];
  include_keywords: string[];
  exclude_keywords: string[];
  exclude_companies: string[];
}

export interface ProfileData {
  values: Record<string, string>;
  missing_required: string[];
  ready: boolean;
  path: string;
}

export interface ResumeInfo {
  resumes: { path: string; filename: string; size: number; sha256: string }[];
  configured: boolean;
  detail?: string;
}

export const api = {
  status: () => request<Status>("/status"),
  profile: () => request<ProfileData>("/profile"),
  saveProfile: (fields: Record<string, string>) =>
    request<{ saved: boolean }>("/profile", {
      method: "POST",
      body: JSON.stringify(fields),
    }),
  resumes: () => request<ResumeInfo>("/resumes"),
  uploadResume: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return fetch(`${BASE}/resumes`, {
      method: "POST",
      body,
      headers: { "X-ApplyOps-Token": sessionToken() },
    }).then((res) => {
      if (!res.ok) throw new Error(`上传失败 (${res.status})`);
      return res.json();
    });
  },
  applications: (stateFilter = "") =>
    request<{ count: number; applications: Application[] }>(
      `/applications${stateFilter ? `?state_filter=${stateFilter}` : ""}`
    ),
  applicationDetail: (id: string) =>
    request<{ application: Application; attempts: Attempt[]; events: { at: string; kind: string }[] }>(
      `/applications/${id}`
    ),
  enqueue: (body: { job_url: string; job_id?: string; title?: string; company?: string }) =>
    request<{ application: Application }>("/applications", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  cancel: (id: string) =>
    request<{ cancelled: boolean }>(`/applications/${id}/cancel`, { method: "POST" }),
  prepare: (id: string) =>
    request<{
      state: string;
      request_id?: string;
      missing?: string[];
      detail?: string;
      fill_report?: FillReport;
    }>(`/applications/${id}/prepare`, { method: "POST" }),
  submit: (id: string, grantId: string) =>
    request<{ status: string; detail: string; evidence: Record<string, unknown> }>(
      `/applications/${id}/submit`,
      { method: "POST", body: JSON.stringify({ grant_id: grantId }) }
    ),
  reconcile: (id: string) =>
    request<{ status: string; detail: string }>(`/applications/${id}/reconcile`, {
      method: "POST",
    }),
  pendingRequests: () => request<{ count: number; requests: PendingRequest[] }>("/requests"),
  approve: (requestId: string) =>
    request<{ approved: boolean; grant_id: string; application_id: string; job_key: string }>(
      `/requests/${requestId}/approve`,
      { method: "POST" }
    ),
  reject: (requestId: string) =>
    request<{ rejected: boolean }>(`/requests/${requestId}/reject`, { method: "POST" }),
  skipRequest: (requestId: string) =>
    request<{ skipped: boolean; application_id: string }>(`/requests/${requestId}/skip`, {
      method: "POST",
    }),
  allowCompany: (requestId: string) =>
    request<{ saved: boolean; company: string; policy: CompanyPolicy }>(
      `/requests/${requestId}/allow-company`,
      { method: "POST" }
    ),
  startDemo: () =>
    request<{ application: Application; demo_url: string }>("/demo/start", { method: "POST" }),

  // ── scoped answers ──
  answers: () =>
    request<{ count: number; revision: string; answers: Record<string, string>[] }>("/answers"),
  saveAnswer: (body: {
    question: string;
    answer: string;
    scope?: string;
    company?: string;
    application_id?: string;
  }) => request<{ saved: boolean }>("/answers", { method: "POST", body: JSON.stringify(body) }),
  answerForApplication: (id: string, question: string, answer: string) =>
    request<{ saved: boolean }>(`/applications/${id}/answer`, {
      method: "POST",
      body: JSON.stringify({ question, answer }),
    }),

  // ── preferences ──
  preferences: () => request<Preferences>("/preferences"),
  savePreferences: (prefs: Preferences) =>
    request<Preferences>("/preferences", { method: "POST", body: JSON.stringify(prefs) }),
  companyPolicy: () => request<CompanyPolicy>("/company-policy"),
  saveCompanyPolicy: (policy: CompanyPolicy) =>
    request<CompanyPolicy>("/company-policy", {
      method: "POST",
      body: JSON.stringify(policy),
    }),
  previewPreference: (body: { title: string; company?: string; location?: string }) =>
    request<{ title: string; keep: boolean; reasons: string[] }>("/preferences/preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // ── runner ──
  runnerStatus: () => request<RunnerStatus>("/runner/status"),
  runnerControl: (action: "pause" | "resume" | "stop") =>
    request<Record<string, boolean>>(`/runner/${action}`, { method: "POST" }),
  runnerPass: (budget: number) =>
    request<{
      budget: number;
      budget_remaining: number;
      prepared: { application_id: string; request_id: string }[];
      submitted: { application_id: string; status: string }[];
      parked: { application_id: string; reason: string }[];
      refused: { application_id: string; reason: string }[];
      stopped_reason: string;
    }>("/runner/pass", { method: "POST" }),
  setPolicy: (body: {
    enabled: boolean;
    max_applications: number;
    allowed_platforms: string[];
    ttl_minutes: number;
  }) => request<Record<string, unknown>>("/runner/policy", { method: "POST", body: JSON.stringify(body) }),
  releaseBrowser: () => request<{ closed: boolean }>("/browser/release", { method: "POST" }),
};

export const STATE_LABELS: Record<string, string> = {
  queued: "排队中",
  preparing: "准备中",
  waiting_for_input: "待你补充",
  waiting_for_approval: "待你批准",
  submitting: "提交中",
  submitted_verified: "已确认提交",
  submitted_unverified: "已发送·未确认",
  failed: "失败",
  cancelled: "已取消",
  skipped: "已跳过",
  legacy_imported: "历史导入",
};
