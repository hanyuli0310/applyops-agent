const BASE = "/api";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
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
  created_at: string;
  summary: string;
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
    return fetch(`${BASE}/resumes`, { method: "POST", body }).then((res) => {
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
    request<{ state: string; request_id?: string; blocked?: string; detail?: string }>(
      `/applications/${id}/prepare`,
      { method: "POST" }
    ),
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
    request<{ approved: boolean; grant_id: string }>(`/requests/${requestId}/approve`, {
      method: "POST",
    }),
  reject: (requestId: string) =>
    request<{ rejected: boolean }>(`/requests/${requestId}/reject`, { method: "POST" }),
  startDemo: () =>
    request<{ application: Application; demo_url: string }>("/demo/start", { method: "POST" }),
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
