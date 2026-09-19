import { useCallback, useEffect, useState } from "react";
import { api, Application, PendingRequest, RunnerStatus, STATE_LABELS } from "../api";
import { browserGrantStorage, rememberGrant } from "../grants";

export function AttentionPage({ onChanged }: { onChanged: () => void }) {
  const [requests, setRequests] = useState<PendingRequest[]>([]);
  const [inputNeeded, setInputNeeded] = useState<Application[]>([]);
  const [unverified, setUnverified] = useState<Application[]>([]);
  const [runner, setRunner] = useState<RunnerStatus | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [passReport, setPassReport] = useState("");
  // How many this pass may send. Empty on purpose: the number is a decision about
  // *this* run, so it starts blank rather than inheriting the last one.
  const [budget, setBudget] = useState("");
  const [policy, setPolicy] = useState({ enabled: false, max_applications: 1, ttl_minutes: 60 });
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const reload = useCallback(() => {
    api.pendingRequests().then((r) => setRequests(r.requests));
    api.applications("waiting_for_input").then((r) => setInputNeeded(r.applications));
    api.applications("submitted_unverified").then((r) => setUnverified(r.applications));
    api.runnerStatus().then((status) => {
      setRunner(status);
      setPolicy({
        enabled: status.policy.enabled,
        max_applications: status.policy.max_applications,
        ttl_minutes: 60,
      });
    });
  }, []);

  useEffect(reload, [reload]);

  const approve = async (requestId: string) => {
    setError("");
    try {
      const res = await api.approve(requestId);
      // The grant is stored against the application it was minted for, so the
      // runs page cannot accidentally spend it on a different application.
      rememberGrant(browserGrantStorage(), {
        applicationId: res.application_id,
        grantId: res.grant_id,
        requestId,
        jobKey: res.job_key,
      });
      setMessage("已批准。回到「运行与记录」页点击提交。");
      reload();
      onChanged();
    } catch (err) {
      setError(String((err as Error).message ?? err));
    }
  };

  const approveAndSubmit = async (request: PendingRequest) => {
    setError("");
    try {
      const approved = await api.approve(request.request_id);
      const result = await api.submit(request.application_id, approved.grant_id);
      setMessage(`已批准并提交 ${request.company || request.job_key}：${result.status}`);
      reload();
      onChanged();
    } catch (err) {
      setError(String((err as Error).message ?? err));
    }
  };

  const reject = async (requestId: string) => {
    await api.reject(requestId);
    reload();
    onChanged();
  };

  const skipRequest = async (requestId: string) => {
    setError("");
    try {
      await api.skipRequest(requestId);
      setMessage("已跳过该申请，并记录原因。");
      reload();
      onChanged();
    } catch (err) {
      setError(String((err as Error).message ?? err));
    }
  };

  const allowCompany = async (requestId: string) => {
    setError("");
    try {
      const result = await api.allowCompany(requestId);
      setMessage(`已将 ${result.company} 从人工确认名单移除；之后的新申请会按默认策略处理。`);
      reload();
      onChanged();
    } catch (err) {
      setError(String((err as Error).message ?? err));
    }
  };

  return (
    <section>
      <h2>待我处理</h2>

      <div className="card">
        <h3>等待你批准的提交（{requests.length}）</h3>
        {requests.length === 0 && <p>没有待批准的提交。</p>}
        {requests.map((r) => (
          <div className="card request" key={r.request_id} data-testid="approval-request">
            <div className="row">
              <strong>{r.company || "未记录公司"}</strong>
              <span className="dim">{r.reason}</span>
            </div>
            <pre className="summary">{r.summary}</pre>
            <p className="hint">批准 = 同意把上面列出的内容原样发给对方，无法撤回。提交仍走现有安全校验。</p>
            <div className="actions">
              <button
                className="primary"
                data-testid="approve-submit-button"
                onClick={() => approveAndSubmit(r)}
              >
                批准并提交
              </button>
              <button data-testid="approve-button" onClick={() => approve(r.request_id)}>
                只批准，稍后提交
              </button>
              <button data-testid="skip-request-button" onClick={() => skipRequest(r.request_id)}>
                跳过
              </button>
              <button data-testid="allow-company-button" onClick={() => allowCompany(r.request_id)}>
                以后允许该公司自动投递
              </button>
            </div>
          </div>
        ))}
      </div>

      <div className="card">
        <h3>等你补充的岗位（{inputNeeded.length}）</h3>
        {inputNeeded.length === 0 && <p>没有缺信息的岗位。</p>}
        {inputNeeded.map((a) => {
          const waiting = runner?.waiting_for_input.find((w) => w.application_id === a.id);
          const missing = waiting?.missing ?? [];
          return (
            <div className="card request" key={a.id} data-testid="needs-input">
              <div className="row">
                <span className="pill warn">{STATE_LABELS[a.state]}</span>
                <strong>{a.title || a.job_key}</strong>
              </div>
              <p className="hint">{waiting?.reason_text ?? "需要补充申请信息"}</p>
              <ul data-testid="missing-list">
                {missing.length === 0 && <li className="dim">（点「重新准备」看看具体缺什么）</li>}
                {missing.map((m) => (
                  <li key={m}>{m}</li>
                ))}
              </ul>
              <div className="actions">
                {missing.slice(0, 1).map((field) => (
                  <span key={field} className="actions">
                    <input
                      data-testid={`answer-${a.id}`}
                      placeholder={`回答「${field}」`}
                      value={drafts[a.id] ?? ""}
                      onChange={(e) => setDrafts({ ...drafts, [a.id]: e.target.value })}
                    />
                    <button
                      data-testid={`save-answer-${a.id}`}
                      onClick={async () => {
                        const answer = (drafts[a.id] ?? "").trim();
                        if (!answer) {
                          setError("先填一个答案再保存。");
                          return;
                        }
                        await api.answerForApplication(a.id, field, answer);
                        setDrafts({ ...drafts, [a.id]: "" });
                        setMessage(`已记录这条答案（只用于这次申请）。点「重新准备」继续。`);
                        reload();
                      }}
                    >
                      保存答案
                    </button>
                  </span>
                ))}
                <button
                  data-testid={`reprepare-${a.id}`}
                  onClick={async () => {
                    try {
                      const res = await api.prepare(a.id);
                      setMessage(
                        res.state === "waiting_for_approval"
                          ? "信息齐了，已生成待批准请求。"
                          : `还缺：${(res.missing ?? []).join("、") || "未知"}`
                      );
                      reload();
                      onChanged();
                    } catch (err) {
                      setError(String((err as Error).message ?? err));
                    }
                  }}
                >
                  重新准备
                </button>
              </div>
            </div>
          );
        })}
      </div>

      <div className="card">
        <h3>批量运行（受监督）</h3>
        <p className="hint">
          运行一轮 = 恢复中断 → 核实未知结果 → 按公司策略准备岗位 →
          普通公司自动提交，人工确认名单停在这里，永不投递名单直接跳过。
        </p>
        <div className="row">
          <span className="pill">{runner?.paused ? "已暂停" : "运行中"}</span>
          <span className="dim">
            自动模式：{runner?.policy_usable ? `开启，上限 ${runner.policy.max_applications} 次` : "关闭（默认）"}
          </span>
        </div>
        <label>
          自动运行总开关
          <select
            data-testid="policy-toggle"
            value={policy.enabled ? "on" : "off"}
            onChange={(e) => setPolicy({ ...policy, enabled: e.target.value === "on" })}
          >
            <option value="off">关闭</option>
            <option value="on">开启</option>
          </select>
        </label>
        <label>
          外层上限（每轮最多提交几份）
          <input
            data-testid="policy-max"
            type="number"
            value={policy.max_applications}
            onChange={(e) => setPolicy({ ...policy, max_applications: Number(e.target.value) })}
          />
        </label>
        <div className="actions">
          <button
            data-testid="policy-save"
            onClick={async () => {
              await api.setPolicy({ ...policy, allowed_platforms: [] });
              setMessage("策略已保存。");
              reload();
            }}
          >
            保存策略
          </button>
          <button data-testid="runner-pause" onClick={async () => { await api.runnerControl("pause"); reload(); }}>
            暂停
          </button>
          <button data-testid="runner-resume" onClick={async () => { await api.runnerControl("resume"); reload(); }}>
            继续
          </button>
          <label>
            这一轮投几份？（每轮都要重新填，不会沿用上次）
            <input
              data-testid="pass-budget"
              type="number"
              min={1}
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
            />
          </label>
          <button
            data-testid="runner-pass"
            disabled={!(Number(budget) > 0)}
            title={Number(budget) > 0 ? "" : "先填这一轮要投几份"}
            onClick={async () => {
              try {
                const report = await api.runnerPass(Number(budget));
                setPassReport(
                  `本轮额度 ${report.budget} · 已投 ${report.submitted.length} · 剩余 ${report.budget_remaining}` +
                    ` · 准备 ${report.prepared.length} · 待补 ${report.parked.length}` +
                    (report.stopped_reason ? ` · ${report.stopped_reason}` : "")
                );
              } catch (err) {
                setError(String((err as Error).message ?? err));
              }
              reload();
              onChanged();
            }}
          >
            立即运行一轮（{Number(budget) > 0 ? `${budget} 份` : "先填数量"}）
          </button>
        </div>
        {passReport && <div className="banner ok" data-testid="pass-report">{passReport}</div>}
      </div>

      <div className="card">
        <h3>已发送但未确认（{unverified.length}）</h3>
        {unverified.length === 0 && <p>没有未知结果的提交。</p>}
        {unverified.map((a) => (
          <div className="row" key={a.id}>
            <span className="pill warn">{STATE_LABELS[a.state]}</span>
            <strong>{a.title || a.job_key}</strong>
            <button
              data-testid={`reconcile-${a.id}`}
              onClick={async () => {
                try {
                  const res = await api.reconcile(a.id);
                  setMessage(`核实结果：${res.status} —— ${res.detail}`);
                  reload();
                  onChanged();
                } catch (err) {
                  setError(String((err as Error).message ?? err));
                }
              }}
            >
              重新核实（不会再次提交）
            </button>
          </div>
        ))}
      </div>

      {message && <div className="banner ok" data-testid="attention-message">{message}</div>}
      {error && <div className="banner error">{error}</div>}
    </section>
  );
}
