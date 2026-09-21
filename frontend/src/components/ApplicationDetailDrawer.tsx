import { useEffect, useMemo, useState } from "react";
import { api, Application, ApplicationEvent, Attempt, PendingRequest } from "../api";
import { browserGrantStorage, forgetGrant, readGrant, rememberGrant } from "../grants";
import { displayCompany, displayTitle, formatTime, stateLabel } from "../dashboard";
import { StatusBadge } from "./StatusBadge";

interface DetailResponse {
  application: Application;
  view?: Application;
  attempts: Attempt[];
  events: ApplicationEvent[];
}

interface ApplicationDetailDrawerProps {
  application: Application;
  request?: PendingRequest;
  onClose: () => void;
  onChanged: () => Promise<void> | void;
}

export function ApplicationDetailDrawer({ application, request, onClose, onChanged }: ApplicationDetailDrawerProps) {
  const [detail, setDetail] = useState<DetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [answer, setAnswer] = useState("");

  const current = detail?.view ?? detail?.application ?? application;
  const missing = useMemo(() => {
    const reason = current.reason_text ?? "";
    if (!reason.includes("：")) return [];
    return reason
      .split("：", 2)[1]
      .split(",")
      .map((item) => item.split(" (")[0].trim())
      .filter(Boolean);
  }, [current.reason_text]);

  const reload = async () => {
    setLoading(true);
    try {
      setDetail(await api.applicationDetail(application.id));
      setError("");
    } catch (err) {
      setError(String((err as Error).message ?? err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void reload();
  }, [application.id]);

  const runAction = async (action: () => Promise<string>) => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      setMessage(await action());
      await reload();
      await onChanged();
    } catch (err) {
      setError(String((err as Error).message ?? err));
    } finally {
      setBusy(false);
    }
  };

  const prepare = () =>
    runAction(async () => {
      const result = await api.prepare(application.id);
      return result.state === "waiting_for_input"
        ? `还缺：${(result.missing ?? []).join("、") || result.detail || "申请信息"}`
        : "表单已准备并验证，等待人工审批。";
    });

  const answerMissing = () => {
    const question = missing[0];
    if (!question || !answer.trim()) {
      setError("先填写一个答案。");
      return;
    }
    void runAction(async () => {
      await api.answerForApplication(application.id, question, answer.trim());
      setAnswer("");
      return `已记录「${question}」的答案。`;
    });
  };

  const approve = () => {
    if (!request) return setError("找不到这份申请的待审批请求，请刷新页面。");
    void runAction(async () => {
      const result = await api.approve(request.request_id);
      rememberGrant(browserGrantStorage(), {
        applicationId: result.application_id,
        grantId: result.grant_id,
        requestId: request.request_id,
        jobKey: result.job_key,
      });
      return "已批准。返回投递列表即可执行提交。";
    });
  };

  const submit = () => {
    const grant = readGrant(browserGrantStorage(), application.id);
    if (!grant) {
      setError("这份申请没有可用的审批令牌，请先批准或重新准备。");
      return;
    }
    void runAction(async () => {
      const result = await api.submit(application.id, grant.grantId);
      if (result.status === "verified") forgetGrant(browserGrantStorage(), application.id);
      return result.status === "verified"
        ? `已确认提交成功：${result.detail}`
        : `结果需要核实：${result.detail}`;
    });
  };

  const reconcile = () =>
    void runAction(async () => {
      const result = await api.reconcile(application.id);
      return `核实结果：${result.status} · ${result.detail}`;
    });

  const cancel = () =>
    void runAction(async () => {
      await api.cancel(application.id);
      return "申请已取消。";
    });

  const skip = () => {
    if (!request) return setError("找不到待处理请求，请刷新页面。");
    void runAction(async () => {
      await api.skipRequest(request.request_id);
      return "申请已跳过。";
    });
  };

  const exportDetail = () => {
    if (!detail) return;
    const blob = new Blob([JSON.stringify(detail, null, 2)], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `applyops-${application.job_key || application.id}.json`;
    link.click();
    URL.revokeObjectURL(link.href);
  };

  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <aside className="detail-drawer" role="dialog" aria-label={`${displayTitle(current)} 详情`}>
        <div className="drawer-header">
          <div>
            <span className="eyebrow">APPLICATION DETAIL</span>
            <h2>{displayTitle(current)}</h2>
            <p className="dim">{displayCompany(current)}{current.location ? ` · ${current.location}` : ""}</p>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="关闭详情">×</button>
        </div>

        {loading && !detail ? <div className="loading-state">正在读取申请详情…</div> : (
          <div className="drawer-content">
            <div className="detail-status-row">
              <StatusBadge state={current.state} />
              <span className="dim">{current.platform || "未知平台"} · {current.route || "未知路线"}</span>
              <span className="dim">更新于 {formatTime(current.updated_at || current.created_at)}</span>
            </div>

            {current.reason_text && <div className={`detail-callout ${current.state === "failed" || current.state === "submitted_unverified" ? "danger" : "info"}`}><strong>{stateLabel(current.state)}</strong><span>{current.reason_text}</span></div>}

            {request && (
              <div className="summary-block">
                <div className="section-heading"><h3>审批摘要</h3><span className="dim">生成于 {formatTime(request.created_at)}</span></div>
                <pre className="summary">{request.summary || "暂无摘要"}</pre>
              </div>
            )}

            {missing.length > 0 && (
              <div className="answer-block">
                <h3>缺少信息</h3>
                <p className="dim">需要先回答一个字段，系统才会继续准备。</p>
                <div className="missing-tags">{missing.map((field) => <span key={field} className="tag">{field}</span>)}</div>
                <div className="inline-form"><input value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder={`回答「${missing[0]}」`} /><button className="primary" onClick={answerMissing} disabled={busy}>保存答案</button></div>
              </div>
            )}

            <div className="drawer-actions">
              {current.available_actions?.includes("prepare") && <button className="primary" onClick={prepare} disabled={busy}>开始准备</button>}
              {current.available_actions?.includes("retry") && <button className="primary" onClick={prepare} disabled={busy}>重新准备</button>}
              {current.available_actions?.includes("approve") && <button className="primary" onClick={approve} disabled={busy}>批准提交</button>}
              {current.available_actions?.includes("reconcile") && <button className="warning-button" onClick={reconcile} disabled={busy}>重新核实</button>}
              {current.state === "waiting_for_approval" && readGrant(browserGrantStorage(), application.id) && <button className="primary" onClick={submit} disabled={busy}>执行提交</button>}
              {request && <button onClick={skip} disabled={busy}>跳过</button>}
              {["queued", "preparing", "waiting_for_input", "waiting_for_approval"].includes(current.state) && <button onClick={cancel} disabled={busy}>取消申请</button>}
            </div>

            {message && <div className="banner ok">{message}</div>}
            {error && <div className="banner error">{error}</div>}

            {detail && <>
              <div className="section-heading"><h3>提交尝试</h3><span className="dim">{detail.attempts.length} 次</span></div>
              {detail.attempts.length === 0 ? <p className="empty-inline">还没有提交尝试。</p> : <div className="attempt-list">{detail.attempts.map((attempt) => <div className="attempt-row" key={attempt.id}><StatusBadge state={attempt.outcome || "preparing"} label={attempt.outcome || "进行中"} /><div><strong>第 {attempt.ordinal} 次</strong><span className="dim">{formatTime(attempt.started_at)}{attempt.ended_at ? ` → ${formatTime(attempt.ended_at)}` : ""}</span></div><span className="attempt-detail">{attempt.detail || "暂无说明"}</span>{attempt.evidence && Object.keys(attempt.evidence).length > 0 && <details><summary>证据</summary><pre className="summary">{JSON.stringify(attempt.evidence, null, 2)}</pre></details>}</div>)}</div>}

              <div className="section-heading timeline-heading"><h3>活动时间线</h3></div>
              {detail.events.length === 0 ? <p className="empty-inline">还没有记录事件。</p> : <ol className="timeline">{detail.events.slice().reverse().map((event, index) => <li key={`${event.at}-${index}`}><span className="timeline-dot" /><div><strong>{event.kind}</strong><span className="dim">{formatTime(event.at)}</span>{typeof event.payload?.detail === "string" && <p>{event.payload.detail}</p>}</div></li>)}</ol>}
            </>}
          </div>
        )}
        <div className="drawer-footer"><button onClick={exportDetail} disabled={!detail}>导出 JSON</button><a href={current.job_url} target="_blank" rel="noreferrer">打开岗位 ↗</a></div>
      </aside>
    </div>
  );
}
