// 页面 2 任务进度 + 页面 3 结果库详情：阶段时间线、关键帧/样片/对比条预览、
// 成片播放/下载、反馈重制（点名窗口，仅重跑相关窗口）、失败重试、日志查看。
// 实时进度经 /ws/progress（App 层 Context）；非终态时 2s 轮询详情与产物清单兜底。

import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api";
import { StageTimeline } from "../components";
import { useProgressState } from "../hooks";
import { STATUS_LABEL, TERMINAL } from "../types";
import type { Artifacts, Task } from "../types";

const EMPTY_ARTIFACTS: Artifacts = { keyframes: [], takes: [], strips: [], finals: [] };

export function TaskDetailPage() {
  const { taskId = "" } = useParams();
  const live = useProgressState();
  const [base, setBase] = useState<Task | null>(null);
  const [artifacts, setArtifacts] = useState<Artifacts>(EMPTY_ARTIFACTS);
  const [fullLog, setFullLog] = useState("");
  const [error, setError] = useState("");

  const [feedbackText, setFeedbackText] = useState("");
  const [windowsText, setWindowsText] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [task, arts] = await Promise.all([api.getTask(taskId), api.artifacts(taskId)]);
      setBase(task);
      setArtifacts(arts);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }, [taskId]);

  useEffect(() => {
    refresh();
    api.log(taskId).then((body) => setFullLog(body.log)).catch(() => undefined);
  }, [refresh, taskId]);

  // WS 事件只带 status/progress 不带 feedback：反馈提交后 base 更新，取两者较新者，
  // 避免 live 快照把刚提交的反馈历史盖掉（浏览器验证实测缺陷）
  const liveTask = live.tasks[taskId];
  const task = liveTask && base
    ? (base.updated_at > liveTask.updated_at ? base : liveTask)
    : liveTask ?? base;
  const status = task?.status;

  // 非终态轮询兜底（WS 断线时进度仍前进）；终态后停
  useEffect(() => {
    if (!status || TERMINAL.includes(status)) return;
    const timer = setInterval(refresh, 2000);
    return () => clearInterval(timer);
  }, [status, refresh]);

  async function sendFeedback(text: string, windows: number[]) {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      const updated = await api.feedback(taskId, text, windows);
      setBase(updated);
      setNotice("反馈已提交，任务重新排队（仅重跑点名窗口）。");
      setFeedbackText("");
      setWindowsText("");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  }

  function submitFeedback(event: FormEvent) {
    event.preventDefault();
    if (!feedbackText.trim()) return setError("反馈内容不能为空");
    const windows = windowsText.split(/[,，\s]+/).filter(Boolean).map(Number);
    if (windows.some((w) => !Number.isInteger(w) || w < 0)) {
      return setError("窗口须为非负整数（逗号分隔，如 0,2）");
    }
    sendFeedback(feedbackText.trim(), windows);
  }

  if (!task) {
    return <p className="muted">{error || "加载中…"}</p>;
  }

  const liveLines = live.logs[taskId] ?? [];
  const logText = [fullLog, ...liveLines].filter(Boolean).join("\n");
  const terminal = TERMINAL.includes(task.status);

  return (
    <div>
      <h1>
        {task.description}
        <span className={`badge ${task.status}`}>{STATUS_LABEL[task.status]}</span>
      </h1>
      <p className="muted">
        {task.task_id} · {task.mode} · 第 {task.rounds} 轮
        {task.budget !== null && ` · 绘图预算 ${task.budget} 次`}
      </p>
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p className="ok" role="status">{notice}</p>}

      <section className="panel">
        <h2>阶段进度</h2>
        <StageTimeline progress={task.progress} />
      </section>

      {artifacts.keyframes.length > 0 && (
        <section className="panel">
          <h2>绘制关键帧（{artifacts.keyframes.length}）</h2>
          <div className="grid">
            {artifacts.keyframes.map((rel) => (
              <figure key={rel}>
                <img src={api.fileUrl(taskId, rel)} alt={rel} loading="lazy" />
                <figcaption>{rel}</figcaption>
              </figure>
            ))}
          </div>
        </section>
      )}

      {artifacts.takes.length > 0 && (
        <section className="panel">
          <h2>窗口样片（{artifacts.takes.length}）</h2>
          <div className="grid">
            {artifacts.takes.map((rel) => (
              <figure key={rel}>
                <video src={api.fileUrl(taskId, rel)} controls preload="metadata" />
                <figcaption>{rel}</figcaption>
              </figure>
            ))}
          </div>
        </section>
      )}

      {artifacts.strips.length > 0 && (
        <section className="panel">
          <h2>审查对比条</h2>
          <div className="grid">
            {artifacts.strips.map((rel) => (
              <figure key={rel}>
                <img src={api.fileUrl(taskId, rel)} alt={rel} loading="lazy" />
                <figcaption>{rel}</figcaption>
              </figure>
            ))}
          </div>
        </section>
      )}

      {terminal && task.final && (
        <section className="panel">
          <h2>成片</h2>
          <video className="player" src={api.resultUrl(taskId)} controls />
          <a className="download" href={api.resultUrl(taskId)} download>下载成片</a>
        </section>
      )}

      {terminal && (
        <section className="panel">
          <h2>反馈重制</h2>
          <p className="muted">指出问题片段，仅重跑反馈点名的窗口，其余沿用已交付样片。</p>
          <form onSubmit={submitFeedback} aria-label="反馈重制" noValidate>
            <label htmlFor="feedbackText">反馈内容</label>
            <textarea id="feedbackText" value={feedbackText}
                      placeholder="例如：第 2 窗角色脸部不像，重新绘制"
                      onChange={(e) => setFeedbackText(e.target.value)} />
            <label htmlFor="windowsText">点名窗口（可选，逗号分隔，如 0,2）</label>
            <input id="windowsText" value={windowsText}
                   onChange={(e) => setWindowsText(e.target.value)} />
            <button type="submit" disabled={busy}>提交反馈</button>
            {task.status === "failed" && (
              <button type="button" className="secondary" disabled={busy}
                      onClick={() => sendFeedback("重试失败任务", [])}>
                失败重试
              </button>
            )}
          </form>
        </section>
      )}

      {task.feedback.length > 0 && (
        <section className="panel">
          <h2>反馈历史</h2>
          <ul>
            {task.feedback.map((entry, index) => (
              <li key={index} className="muted">
                第 {index + 1} 次反馈：{entry.text}
                {entry.windows.length > 0 && `（窗口 ${entry.windows.join(",")}）`}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="panel">
        <h2>运行日志</h2>
        <pre className="log" data-testid="task-log">{logText || "（暂无日志）"}</pre>
      </section>
    </div>
  );
}