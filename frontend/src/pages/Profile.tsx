import { useCallback, useEffect, useState } from "react";
import { api, ProfileData, ResumeInfo } from "../api";

export function ProfilePage({ onChanged }: { onChanged: () => void }) {
  const [profile, setProfile] = useState<ProfileData | null>(null);
  const [resume, setResume] = useState<ResumeInfo | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const reload = useCallback(() => {
    api.profile().then((p) => {
      setProfile(p);
      setDraft(p.values);
    });
    api.resumes().then(setResume);
  }, []);

  useEffect(reload, [reload]);

  const save = async () => {
    setError("");
    setMessage("");
    try {
      const onlyFilled = Object.fromEntries(
        Object.entries(draft).filter(([, v]) => v.trim() !== "")
      );
      await api.saveProfile(onlyFilled);
      setMessage("已保存。改动立即生效。");
      reload();
      onChanged();
    } catch (err) {
      setError(String((err as Error).message ?? err));
    }
  };

  const upload = async (file: File) => {
    setError("");
    setMessage("");
    try {
      const res = await api.uploadResume(file);
      setMessage(`简历已保存：${res.describe}`);
      reload();
      onChanged();
    } catch (err) {
      setError(String((err as Error).message ?? err));
    }
  };

  if (!profile) return <p>加载中…</p>;

  const missing = new Set(profile.missing_required);

  return (
    <section>
      <h2>资料与简历</h2>
      <p className="hint">
        空着的字段会在真实填表时被问到你，系统绝不代猜。薪资、签证状态这类「答错有实害」的项请自己确认。
      </p>

      <div className="card">
        <h3>简历（唯一一份，上传即替换）</h3>
        {resume?.configured ? (
          <p>
            当前：{resume.resumes[0].filename}（{resume.resumes[0].size.toLocaleString()} 字节，
            sha256 {resume.resumes[0].sha256.slice(0, 12)}…）
          </p>
        ) : (
          <p className="warn">尚未配置简历{resume?.detail ? `：${resume.detail}` : ""}</p>
        )}
        <input
          type="file"
          accept=".pdf,.doc,.docx"
          data-testid="resume-input"
          onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
        />
      </div>

      <div className="card">
        <h3>个人事实</h3>
        {Object.entries(profile.values).length === 0 && (
          <p className="warn">还没有任何资料。下面直接填写即可。</p>
        )}
        {Object.entries(profile.values).map(([key, value]) => (
          <label key={key}>
            {key}
            {missing.has(key) && <span className="badge">必填</span>}
            <input
              data-testid={`profile-${key}`}
              value={draft[key] ?? ""}
              onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
              placeholder={value || "未填写"}
            />
          </label>
        ))}
        <div className="actions">
          <button onClick={save} data-testid="profile-save">
            保存
          </button>
        </div>
      </div>

      {message && <div className="banner ok">{message}</div>}
      {error && <div className="banner error">{error}</div>}
      <p className="hint">档案文件：{profile.path}（可直接手改，立即生效）</p>
    </section>
  );
}
