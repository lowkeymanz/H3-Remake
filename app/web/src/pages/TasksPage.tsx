// 页面 1 任务创建 + 历史任务选择（需求八页面 3 的结果库列表）。
// 创建口径与 POST /api/tasks 逐字对齐：multipart video/description/mode/budget/start/dur/cast；
// swap 支持多人映射：动态增删 cast 行（源人物 id + 角色库点选），每行参考图可选（作为该角色 photo）。
// 历史任务：整行卡片跳转 /tasks/:id 详情页（查看 + 反馈重跑均在详情页），与创建表单
// 分属两个路由两套 state——点选历史任务即卸载本组件，新建表单参数不可能与之穿插。

import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { useProgressState } from "../hooks";
import { STATUS_LABEL, TERMINAL } from "../types";
import type { CharEntry, Mode, Task } from "../types";

interface CastRow {
  sourceId: string;
  charName: string;
  refPhoto: File | null;
}

const emptyRow = (): CastRow => ({ sourceId: "", charName: "", refPhoto: null });

// 历史任务卡片的创建时间（epoch 秒 → 本地"月-日 时:分"）
function formatTime(epoch: number): string {
  const d = new Date(epoch * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function TasksPage() {
  const navigate = useNavigate();
  const live = useProgressState();
  const [fetched, setFetched] = useState<Task[]>([]);
  const [chars, setChars] = useState<CharEntry[]>([]);
  const [budgetDefault, setBudgetDefault] = useState<number>(24);

  const [video, setVideo] = useState<File | null>(null);
  const [mode, setMode] = useState<Mode>("edit");
  const [description, setDescription] = useState("");
  const [budget, setBudget] = useState("");
  const [start, setStart] = useState("0");
  const [dur, setDur] = useState("");
  const [castRows, setCastRows] = useState<CastRow[]>([emptyRow()]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const patchRow = (index: number, patch: Partial<CastRow>) =>
    setCastRows((rows) => rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));

  useEffect(() => {
    api.listTasks().then(setFetched).catch(() => undefined);
    api.chars().then(setChars).catch(() => undefined);
    api.settings().then((s) => {
      setBudgetDefault(s.budget_default);
      setBudget((prev) => prev || String(s.budget_default));
    }).catch(() => undefined);
  }, []);

  // WS 快照与 REST 全量按 task_id 合并取 updated_at 新者：断线重连边缘场景下
  // 两边状态可能不同步，覆盖式写法会丢更新或丢任务
  const merged = new Map<string, Task>();
  for (const task of fetched) merged.set(task.task_id, task);
  for (const task of Object.values(live.tasks)) {
    const prev = merged.get(task.task_id);
    if (!prev || task.updated_at >= prev.updated_at) merged.set(task.task_id, task);
  }
  const sorted = [...merged.values()].sort((a, b) => b.created_at - a.created_at);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (!video) return setError("请选择源视频");
    if (!description.trim()) return setError("请填写任务描述");
    if (mode === "swap") {
      if (castRows.some((row) => !row.sourceId.trim() || !row.charName)) {
        return setError("swap 任务须填写源人物 id 并点选角色库角色（每行均须填全）");
      }
      const ids = castRows.map((row) => row.sourceId.trim());
      if (new Set(ids).size !== ids.length) return setError("源人物 id 不能重复");
    }
    const form = new FormData();
    form.append("video", video);
    form.append("description", description.trim());
    form.append("mode", mode);
    if (budget) form.append("budget", budget);
    if (start) form.append("start", start);
    if (dur) form.append("dur", dur);
    const cast = mode === "swap"
      ? Object.fromEntries(castRows.map((row) => [row.sourceId.trim(), row.charName]))
      : {};
    form.append("cast", JSON.stringify(cast));
    setBusy(true);
    try {
      const task = await api.createTask(form);
      if (mode === "swap") {
        for (const row of castRows) {
          if (row.refPhoto && row.charName) {
            await api.uploadPhoto(row.charName, row.refPhoto).catch(() => undefined);
          }
        }
      }
      navigate(`/tasks/${task.task_id}`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h1>创建任务</h1>
      <form className="panel" onSubmit={submit} aria-label="创建任务" noValidate>
        <label htmlFor="video">源视频（mp4/mov/mkv/webm/avi）</label>
        <input id="video" type="file" accept=".mp4,.mov,.mkv,.webm,.avi"
               onChange={(e) => setVideo(e.target.files?.[0] ?? null)} />
        <label htmlFor="mode">模式</label>
        <select id="mode" value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
          <option value="edit">edit（一句话编辑）</option>
          <option value="swap">swap（角色替换）</option>
        </select>
        <label htmlFor="description">任务描述</label>
        <textarea id="description" value={description}
                  placeholder={mode === "edit" ? "例如：把涡轮机去掉" : "例如：把主角换成角色库角色"}
                  onChange={(e) => setDescription(e.target.value)} />
        {mode === "swap" && (
          <div className="castrows">
            {castRows.map((row, i) => (
              <div className="row" key={i}>
                <div>
                  <label htmlFor={`sourceId-${i}`}>源人物 id（源片中的人物标识）</label>
                  <input id={`sourceId-${i}`} value={row.sourceId}
                         onChange={(e) => patchRow(i, { sourceId: e.target.value })} />
                </div>
                <div>
                  <label htmlFor={`charName-${i}`}>目标角色（角色库点选）</label>
                  <select id={`charName-${i}`} value={row.charName}
                          onChange={(e) => patchRow(i, { charName: e.target.value })}>
                    <option value="">请选择角色</option>
                    {chars.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
                  </select>
                </div>
                <div>
                  <label htmlFor={`refPhoto-${i}`}>参考图（可选，作为该角色 photo）</label>
                  <input id={`refPhoto-${i}`} type="file" accept="image/*"
                         onChange={(e) => patchRow(i, { refPhoto: e.target.files?.[0] ?? null })} />
                </div>
                <button type="button" className="danger" disabled={castRows.length === 1}
                        onClick={() => setCastRows((rows) => rows.filter((_, j) => j !== i))}>
                  移除
                </button>
              </div>
            ))}
            <button type="button" onClick={() => setCastRows((rows) => [...rows, emptyRow()])}>
              添加人物映射
            </button>
          </div>
        )}
        <div className="row">
          <div>
            <label htmlFor="start">起始秒（可选，默认 0）</label>
            <input id="start" type="number" min="0" step="0.1" value={start}
                   onChange={(e) => setStart(e.target.value)} />
          </div>
          <div>
            <label htmlFor="dur">时长秒（可选，默认全片）</label>
            <input id="dur" type="number" min="0.1" step="0.1" value={dur}
                   onChange={(e) => setDur(e.target.value)} />
          </div>
          <div>
            <label htmlFor="budget">绘图预算上限（次，默认 {budgetDefault}）</label>
            <input id="budget" type="number" min="1" value={budget}
                   onChange={(e) => setBudget(e.target.value)} />
            <p className="formhint">计本任务调用云端图像生成 API 的次数（生成角色表、绘制关键帧、重绘），
              按量付费；从角色库取表与本地 GPU 渲染不占预算。达上限后停止绘图、按已绘内容交付；
              单人替换典型用量 2–6 次。</p>
          </div>
        </div>
        {error && <p className="error" role="alert">{error}</p>}
        <button type="submit" disabled={busy}>{busy ? "提交中…" : "创建任务"}</button>
      </form>

      <h1>历史任务</h1>
      {sorted.length === 0 && <p className="muted">暂无任务</p>}
      <ul className="tasklist">
        {sorted.map((task) => (
          <li key={task.task_id} className="taskcard" data-testid="task-card">
            <Link className="taskcard-main" to={`/tasks/${task.task_id}`}
                  aria-label={`查看任务：${task.description}`}>
              <span className="taskcard-desc">{task.description}</span>
              <span className="taskcard-meta">
                <span className={`badge ${task.status}`}>{STATUS_LABEL[task.status]}</span>
                {task.mode} · 第 {task.rounds} 轮 · {formatTime(task.created_at)}
              </span>
            </Link>
            {TERMINAL.includes(task.status) && task.final && (
              <a className="download" href={api.resultUrl(task.task_id)} download>下载成片</a>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}