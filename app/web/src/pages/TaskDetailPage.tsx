// 页面 2 任务进度 + 页面 3 结果库详情：阶段时间线、产物预览（可放大）、渲染窗口清单、
// 成片播放/下载、反馈重制（点选窗口）、失败重试、日志查看。
// 实时进度经 /ws/progress（App 层 Context）；非终态时 2s 轮询详情与产物清单兜底。

import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api";
import { formatClock, StageTimeline, ZoomableImage } from "../components";
import { useProgressState } from "../hooks";
import { STATUS_LABEL, TERMINAL } from "../types";
import type { Artifacts, Task, WindowInfo } from "../types";

const EMPTY_ARTIFACTS: Artifacts = { keyframes: [], takes: [], strips: [], finals: [], windows: [] };
const TAKE_FILE = /w(\d+)_(\d+)_s(\d+)/;

function stripLabel(rel: string): string {
  const file = rel.split("/").pop() ?? rel;
  const match = /_w(\d+)\.jpg$/.exec(file);
  return `${match ? `w${match[1]}` : file} · ${file.split("_")[0]} 审查对比条`;
}

function takeLabel(rel: string, windows: WindowInfo[]): string {
  const file = rel.split("/").pop() ?? rel;
  const match = TAKE_FILE.exec(file);
  if (!match) return file;
  const [, index, canvas, seed] = match;
  const chosen = windows[Number(index)]?.picked_seed === Number(seed) ? " · 最终选用" : "";
  return `w${index} · ${canvas} · seed ${seed}${chosen}`;
}

function windowCaption(info: WindowInfo, sourceStart: number): string {
  const [a, b] = info.own_s;
  const state = info.passthrough ? "未渲染（沿用源片）"
    : info.picked_seed !== null ? `已交付 seed ${info.picked_seed}` : "待渲染 / 未选用";
  const grade = info.grade ? ` · 验收 ${info.grade}${info.note ? `（${info.note}）` : ""}` : "";
  return `${formatClock(a)}–${formatClock(b)} · 源片 ${formatClock(sourceStart + a)}–${formatClock(sourceStart + b)} · ${state}${grade}`;
}

export function TaskDetailPage() {
  const { taskId = "" } = useParams();
  const live = useProgressState();
  const [base, setBase] = useState<Task | null>(null);
  const [artifacts, setArtifacts] = useState<Artifacts>(EMPTY_ARTIFACTS);
  const [fullLog, setFullLog] = useState("");
  const [error, setError] = useState("");

  const [feedbackText, setFeedbackText] = useState("");
  const [manualWindows, setManualWindows] = useState("");  // 无窗口清单时的手输兜底
  const [picked, setPicked] = useState<number[]>([]);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [currentWindow, setCurrentWindow] = useState<number | null>(null);
  const player = useRef<HTMLVideoElement>(null);

  const refresh = useCallback(async () => {
    try {
      const [task, arts] = await Promise.all([api.getTask(taskId), api.artifacts(taskId)]);
      setBase(task);
      setArtifacts({ ...EMPTY_ARTIFACTS, ...arts });  // 旧后端无 windows 字段时降级为空清单
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

  const windows = artifacts.windows;

  async function sendFeedback(text: string, selected: number[]) {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      const updated = await api.feedback(taskId, text, selected);
      setBase(updated);
      setNotice("反馈已提交，任务重新排队（仅重跑点名窗口）。");
      setFeedbackText("");
      setManualWindows("");
      setPicked([]);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  }

  function submitFeedback(event: FormEvent) {
    event.preventDefault();
    if (!feedbackText.trim()) return setError("反馈内容不能为空");
    if (windows.length === 0) {  // 尚未切窗：保留手输兜底
      const parsed = manualWindows.split(/[,，\s]+/).filter(Boolean).map(Number);
      if (parsed.some((w) => !Number.isInteger(w) || w < 0)) {
        return setError("窗口须为非负整数（逗号分隔，如 0,2）");
      }
      return sendFeedback(feedbackText.trim(), parsed);
    }
    if (picked.some((w) => w < 0 || w >= windows.length)) {
      return setError(`窗口编号须在 0..${windows.length - 1}`);
    }
    return sendFeedback(feedbackText.trim(), picked);
  }

  function toggleWindow(index: number) {
    setPicked((prev) => prev.includes(index)
      ? prev.filter((w) => w !== index)
      : [...prev, index].sort((a, b) => a - b));
  }

  function seekTo(info: WindowInfo) {
    if (player.current) player.current.currentTime = info.own_s[0];
    setCurrentWindow(info.index);
  }

  function onTimeUpdate() {
    const time = player.current?.currentTime;
    if (time === undefined || windows.length === 0) return;
    const found = windows.find((info) => time >= info.own_s[0] && time < info.own_s[1]);
    setCurrentWindow(found ? found.index : null);
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
        {` · 源片处理区间 ${formatClock(task.start)}`}
        {task.dur !== null ? `–${formatClock(task.start + task.dur)}` : " 起（全片）"}
      </p>
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p className="ok" role="status">{notice}</p>}

      <section className="panel">
        <h2>阶段进度</h2>
        <StageTimeline progress={task.progress} />
      </section>

      {windows.length > 0 && (
        <section className="panel" data-testid="window-list">
          <h2>渲染窗口（{windows.length}）</h2>
          <p className="muted">编号从 w0 开始（w0 = 第 1 段）；前者为成片内时间码，括号内为源片位置。点「定位」跳到成片对应时刻。</p>
          <ul className="windows">
            {windows.map((info) => (
              <li key={info.index} data-testid={`window-${info.index}`}
                  className={`window-card${currentWindow === info.index ? " active" : ""}${picked.includes(info.index) ? " selected" : ""}`}>
                <strong>w{info.index}</strong>
                <span>{windowCaption(info, task.start)}</span>
                <button type="button" className="secondary" onClick={() => seekTo(info)}>定位</button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {artifacts.keyframes.length > 0 && (
        <section className="panel">
          <h2>绘制关键帧（{artifacts.keyframes.length}）</h2>
          <div className="grid">
            {artifacts.keyframes.map((rel) => (
              <figure key={rel}>
                <ZoomableImage src={api.fileUrl(taskId, rel)} alt={rel} label={rel} />
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
                <span className="artifact-label">{takeLabel(rel, windows)}</span>
                <video src={api.fileUrl(taskId, rel)} controls preload="metadata" />
                <figcaption>{rel}</figcaption>
              </figure>
            ))}
          </div>
        </section>
      )}

      {artifacts.strips.length > 0 && (
        <section className="panel">
          <h2>审查对比条（点击放大，按窗口）</h2>
          <div className="grid">
            {artifacts.strips.map((rel) => (
              <figure key={rel}>
                <span className="artifact-label">{stripLabel(rel)}</span>
                <ZoomableImage src={api.fileUrl(taskId, rel)} alt={rel} label={stripLabel(rel)} />
                <figcaption>{rel}</figcaption>
              </figure>
            ))}
          </div>
        </section>
      )}

      {terminal && task.final && (
        <section className="panel">
          <h2>成片</h2>
          <video className="player" ref={player} src={api.resultUrl(taskId)} controls
                 onTimeUpdate={onTimeUpdate} />
          {currentWindow !== null && windows[currentWindow] && (
            <p className="formhint">当前播放：w{currentWindow}（{formatClock(windows[currentWindow].own_s[0])}–{formatClock(windows[currentWindow].own_s[1])}）</p>
          )}
          <a className="download" href={api.resultUrl(taskId)} download>下载成片</a>
        </section>
      )}

      {terminal && (
        <section className="panel">
          <h2>反馈重制</h2>
          <p className="muted">成片交付后可提交反馈；勾选出问题的窗口，仅重跑这些窗口，其余沿用已交付样片。编号从 w0 开始（w0 = 第 1 段）。</p>
          <form onSubmit={submitFeedback} aria-label="反馈重制" noValidate>
            <label htmlFor="feedbackText">反馈内容</label>
            <textarea id="feedbackText" value={feedbackText}
                      placeholder="例如：w1 里角色脸部不像，请重绘"
                      onChange={(e) => setFeedbackText(e.target.value)} />
            {/第\s*\d+\s*窗/.test(feedbackText) && (
              <p className="formhint">文字里的「第 N 窗」不会被解析，请在下方勾选窗口（w0 为第 1 段）。</p>
            )}
            {windows.length > 0 ? (
              <>
                <p className="formhint">点名窗口（可多选；不勾选 = 由 agent 依据文字判断）</p>
                <div className="window-chips" data-testid="window-chips">
                  {windows.map((info) => (
                    <label key={info.index}
                           className={`window-chip${picked.includes(info.index) ? " selected" : ""}`}>
                      <input type="checkbox" checked={picked.includes(info.index)}
                             onChange={() => toggleWindow(info.index)} />
                      {`w${info.index} ${formatClock(info.own_s[0])}–${formatClock(info.own_s[1])}`}
                    </label>
                  ))}
                </div>
                {currentWindow !== null && (
                  <button type="button" className="secondary" onClick={() => toggleWindow(currentWindow)}>
                    点名当前播放窗口 w{currentWindow}
                  </button>
                )}
                <p className="formhint">已点名：{picked.length ? picked.map((w) => `w${w}`).join("、") : "无（由 agent 判断）"}</p>
              </>
            ) : (
              <>
                <label htmlFor="windowsText">点名窗口（可选，逗号分隔，如 0,2；w0 为第 1 段）</label>
                <input id="windowsText" value={manualWindows}
                       onChange={(e) => setManualWindows(e.target.value)} />
              </>
            )}
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
                {entry.windows.length > 0 && `（${entry.windows.map((w) => {
                  const info = windows[w];
                  return info ? `w${w} ${formatClock(info.own_s[0])}–${formatClock(info.own_s[1])}` : `窗口 ${w}`;
                }).join("、")}）`}
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
