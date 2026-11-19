// 页面 1 任务创建 + 任务列表（历史任务入口，需求八页面 3 的结果库列表）。
// 创建口径与 POST /api/tasks 逐字对齐：multipart video/description/mode/budget/start/dur/cast；
// swap 点选角色库角色自动并入 cast；参考图可选，作为所选角色的 photo 来源。

import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { useProgressState } from "../hooks";
import { STATUS_LABEL, TERMINAL } from "../types";
import type { CharEntry, Mode, Task } from "../types";

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
  const [sourceId, setSourceId] = useState("");
  const [charName, setCharName] = useState("");
  const [refPhoto, setRefPhoto] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.listTasks().then(setFetched).catch(() => undefined);
    api.chars().then(setChars).catch(() => undefined);
    api.settings().then((s) => {
      setBudgetDefault(s.budget_default);
      setBudget((prev) => prev || String(s.budget_default));
    }).catch(() => undefined);
  }, []);

  const tasks = Object.keys(live.tasks).length > 0
    ? Object.values(live.tasks)
    : fetched;
  const sorted = [...tasks].sort((a, b) => b.created_at - a.created_at);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (!video) return setError("请选择源视频");
    if (!description.trim()) return setError("请填写任务描述");
    if (mode === "swap" && (!sourceId.trim() || !charName)) {
      return setError("swap 任务须填写源人物 id 并点选角色库角色");
    }
    const form = new FormData();
    form.append("video", video);
    form.append("description", description.trim());
    form.append("mode", mode);
    if (budget) form.append("budget", budget);
    if (start) form.append("start", start);
    if (dur) form.append("dur", dur);
    form.append("cast", mode === "swap" ? JSON.stringify({ [sourceId.trim()]: charName }) : "{}");
    setBusy(true);
    try {
      const task = await api.createTask(form);
      if (mode === "swap" && refPhoto && charName) {
        await api.uploadPhoto(charName, refPhoto).catch(() => undefined);
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
          <div className="row">
            <div>
              <label htmlFor="sourceId">源人物 id（源片中的人物标识）</label>
              <input id="sourceId" value={sourceId} onChange={(e) => setSourceId(e.target.value)} />
            </div>
            <div>
              <label htmlFor="charName">目标角色（角色库点选）</label>
              <select id="charName" value={charName} onChange={(e) => setCharName(e.target.value)}>
                <option value="">请选择角色</option>
                {chars.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
              </select>
            </div>
            <div>
              <label htmlFor="refPhoto">参考图（可选，作为该角色 photo）</label>
              <input id="refPhoto" type="file" accept="image/*"
                     onChange={(e) => setRefPhoto(e.target.files?.[0] ?? null)} />
            </div>
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
            <label htmlFor="budget">预算上限（张，默认 {budgetDefault}）</label>
            <input id="budget" type="number" min="1" value={budget}
                   onChange={(e) => setBudget(e.target.value)} />
          </div>
        </div>
        {error && <p className="error" role="alert">{error}</p>}
        <button type="submit" disabled={busy}>{busy ? "提交中…" : "创建任务"}</button>
      </form>

      <h1>任务列表</h1>
      {sorted.length === 0 && <p className="muted">暂无任务</p>}
      <ul className="tasklist">
        {sorted.map((task) => (
          <li key={task.task_id}>
            <Link to={`/tasks/${task.task_id}`}>{task.description}</Link>
            <span className={`badge ${task.status}`}>{STATUS_LABEL[task.status]}</span>
            <span className="muted"> {task.mode} · 第 {task.rounds} 轮</span>
            {TERMINAL.includes(task.status) && task.final && (
              <a className="download" href={api.resultUrl(task.task_id)} download>下载成片</a>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}