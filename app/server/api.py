"""Web 后端 API（M5.1）：任务 CRUD + 上传落盘 + 角色库 + 反馈重制 + 结果下载。

需求八的 v1 口径（单用户工具，无鉴权）：
- 任务创建：上传视频落盘 $REMAKE_HOME/uploads/<task_id>/，作为 agent peek/ingest 的源；
  task_id 即 MCP session id（字母/数字/-，与 new_session 的 sid 校验同口径），预建
  sessions/<task_id>/ 并写 kind 文件（与 new_session 落位逐字一致，MCP 侧幂等不重置）；
  预算写入该会话目录的 paint_limit 文件（原项目 per-session 覆盖机制，paint.py limit()）；
- 进度：读 sessions/<task_id>/ 产物推导阶段时间线（读片→规划→绘制→渲染→审查→拼片），
  running 任务对盘上 result.json 重新裁决（交付同步终态）；WS 实时推送见 /ws/progress
  （M5.2：事件总线 + 进程池，start_pool=True 时启用）；
- 反馈重制：终态任务追加反馈、rounds+1、拨回 created 复用原 session，follow-up 提示
  只重跑反馈点名的窗口（原项目 AGENTS.md follow-up round 机制），驱动由 5.2 进程池承接；
- 重启恢复：create_app 时 store.recover()——running 已交付同步终态，未交付标 interrupted
  （M4.4 看门狗续跑的输入）；
- 静态服务：/files/sessions 与 /files/chars 直出中间产物（关键帧、样片、角色表）；
  web_dist 传入前端构建产物时另提供 SPA 静态服务（M5.4 阶段 9 服务入口）。
"""

from __future__ import annotations

import asyncio
import json
import queue
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.agent.watchdog import result_status
from app.server import chars, settings
from app.server.pool import AgentPool, EventBus
from app.server.prompts import followup_prompt, session_progress, task_prompt
from app.server.store import TERMINAL, Task, TaskStore
from scripts.config import ConfigError, resolve_remake_home

VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
TASK_ID_RE = re.compile(r"[A-Za-z0-9-]+")  # MCP sid 口径：字母/数字/-（队列按 _<sid>_ 匹配）


def new_task_id() -> str:
    return "t" + uuid.uuid4().hex[:12]


def _sync_running(store: TaskStore, task: Task, session_dir: Path) -> Task:
    """running 任务对盘上 result.json 重新裁决：已交付同步终态与成片路径。"""
    if task.status != "running":
        return task
    status = result_status(session_dir)
    if status not in TERMINAL:
        return task
    try:
        result = json.loads((session_dir / "result.json").read_text(encoding="utf-8"))
        final = str(result.get("final") or "")
    except (OSError, ValueError):
        final = ""
    return store.set_status(task.task_id, status, final=final)


def create_app(root: Path, home: Path | None = None, *, start_pool: bool = False,
               pool_drive=None, max_workers: int = 1, max_rounds: int = 3,
               ticker_interval: float = 2.0, web_dist: Path | None = None) -> FastAPI:
    """组装后端：root 供 Hermes/MCP 定位运行时，home 缺省按 config.toml/环境解析。

    start_pool=True 启用 M5.2 agent 进程池（领取 created/interrupted 任务，看门狗驱动，
    事件经总线由 /ws/progress 下发）；pool_drive 注入假驱动供零费测试，缺省走真实
    Hermes × MCP（config.toml [llm] 三元组）。web_dist 传入前端构建产物目录（M5.4）：
    SPA 静态服务，客户端路由回退 index.html；缺省/未构建时仅提供 API。
    """
    root = Path(root)
    home = Path(home) if home is not None else resolve_remake_home(root)
    for sub in ("", "sessions", "chars", "uploads"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    store = TaskStore(home / "tasks.db")
    store.recover(lambda sid: home / "sessions" / sid)  # 重启恢复（需求八）
    bus = EventBus()
    pool = (AgentPool(root, home, store, bus, drive=pool_drive, max_workers=max_workers,
                      max_rounds=max_rounds, ticker_interval=ticker_interval)
            if start_pool else None)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if pool is not None:
            pool.start()
        yield
        if pool is not None:
            pool.stop()
        store.close()

    app = FastAPI(title="H3-Remake", lifespan=lifespan)
    app.state.root, app.state.home, app.state.store = root, home, store
    app.state.bus, app.state.pool = bus, pool

    def task_of(task_id: str) -> Task:
        task = store.get(task_id)
        if task is None:
            raise HTTPException(404, f"任务 {task_id} 不存在")
        return task

    # ── 任务 API ─────────────────────────────────────────────────────────────
    @app.post("/api/tasks", status_code=201)
    async def create_task(video: UploadFile = File(...), description: str = Form(...),
                          mode: str = Form("edit"), budget: int | None = Form(None),
                          start: float = Form(0.0), dur: float | None = Form(None),
                          cast: str = Form("{}")):
        if mode not in ("swap", "edit"):
            raise HTTPException(400, "mode 须为 swap 或 edit")
        if not description.strip():
            raise HTTPException(400, "任务描述不能为空")
        try:
            cast_map = json.loads(cast)
        except ValueError:
            raise HTTPException(400, "cast 不是合法 JSON")
        if not isinstance(cast_map, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                     for k, v in cast_map.items()):
            raise HTTPException(400, "cast 须为 {源人物 id: 角色名} 映射")
        if mode == "swap" and not cast_map:
            raise HTTPException(400, "swap 任务须给出 cast（源人物 -> 角色库角色）")
        if budget is not None and budget < 1:
            raise HTTPException(400, "budget 须为正整数")
        if start < 0 or (dur is not None and dur <= 0):
            raise HTTPException(400, "start 须 >= 0，dur 须 > 0")
        suffix = Path(video.filename or "").suffix.lower()
        if suffix not in VIDEO_SUFFIXES:
            raise HTTPException(400, f"不支持的视频格式 {suffix!r}（{'/'.join(sorted(VIDEO_SUFFIXES))}）")
        data = await video.read()
        if not data:
            raise HTTPException(400, "上传视频为空")
        task_id = new_task_id()
        upload_dir = home / "uploads" / task_id
        upload_dir.mkdir(parents=True)
        src = upload_dir / f"source{suffix}"
        src.write_bytes(data)
        # 预建会话目录（与 MCP new_session 落位一致：kind 文件须在，幂等检查读它）；
        # 预算走 per-session paint_limit 覆盖（paint.py limit() 先读文件再读环境）
        session_dir = home / "sessions" / task_id
        session_dir.mkdir()
        (session_dir / "kind").write_text(mode)
        if budget is not None:
            (session_dir / "paint_limit").write_text(str(budget))
        task = store.create(Task(task_id=task_id, mode=mode, description=description.strip(),
                                 src=str(src), cast=cast_map, budget=budget,
                                 start_s=start, dur_s=dur))
        return task.to_dict()

    @app.get("/api/tasks")
    def list_tasks():
        return [task.to_dict() for task in store.list()]

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str):
        task = _sync_running(store, task_of(task_id), home / "sessions" / task_id)
        return {**task.to_dict(), "progress": session_progress(home / "sessions" / task_id)}

    @app.get("/api/tasks/{task_id}/result")
    def download_result(task_id: str):
        task = _sync_running(store, task_of(task_id), home / "sessions" / task_id)
        final = Path(task.final)
        if task.status not in TERMINAL or not task.final or not final.is_file():
            raise HTTPException(409, f"任务 {task_id} 尚未交付成片（状态 {task.status}）")
        return FileResponse(final, media_type="video/mp4", filename=final.name)

    @app.get("/api/tasks/{task_id}/log")
    def get_task_log(task_id: str):
        task_of(task_id)
        path = home / "logs" / f"{task_id}.log"
        return {"task_id": task_id,
                "log": path.read_text(encoding="utf-8") if path.is_file() else ""}

    @app.get("/api/tasks/{task_id}/artifacts")
    def get_artifacts(task_id: str):
        """中间产物清单（M5.3 预览）：与 session_progress 同口径，相对会话目录路径，
        前端拼 /files/sessions/<task_id>/<rel> 直出。"""
        task_of(task_id)
        case = home / "sessions" / task_id / "case"

        def rels(paths) -> list[str]:
            return sorted(p.relative_to(home / "sessions" / task_id).as_posix() for p in paths)

        return {
            "keyframes": rels(p for p in case.glob("paint/*/f*.png") if p.parent.name != "src"),
            "takes": rels(case.glob("takes/*.mp4")),
            "strips": rels(case.glob("look/strips_*.jpg")),
            "finals": rels(case.glob("final/full_*.mp4")),
        }

    # ── 反馈重制 API ─────────────────────────────────────────────────────────
    @app.post("/api/tasks/{task_id}/feedback")
    def submit_feedback(task_id: str, body: dict):
        task = task_of(task_id)
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "反馈内容不能为空")
        windows = body.get("windows") or []
        if not isinstance(windows, list) or not all(isinstance(w, int) and w >= 0 for w in windows):
            raise HTTPException(400, "windows 须为非负整数数组（反馈点名的窗口）")
        entry = {"text": text, "windows": windows, "at": time.time()}
        try:
            task = store.add_feedback(task_id, entry)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {**task.to_dict(), "prompt": followup_prompt(task)}

    # ── 角色库 API ───────────────────────────────────────────────────────────
    @app.get("/api/chars")
    def get_chars():
        return chars.list_chars(home / "chars")

    @app.post("/api/chars/{name}/sheet")
    async def upload_sheet(name: str, file: UploadFile = File(...),
                           desc: str = Form(""), outfit: str = Form("")):
        try:
            return chars.add_sheet(home / "chars", name, await file.read(), desc=desc, outfit=outfit)
        except chars.CharsError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/chars/{name}/photo")
    async def upload_photo(name: str, file: UploadFile = File(...)):
        try:
            return chars.add_photo(home / "chars", name, await file.read())
        except chars.CharsError as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/chars/{name}")
    def delete_char(name: str):
        try:
            chars.remove_char(home / "chars", name)
        except chars.CharsError as exc:
            raise HTTPException(404, str(exc))
        return {"deleted": name}

    # ── 设置 API（M5.3 页面 4：LLM/Painter 配置与预算默认值）────────────────
    @app.get("/api/settings")
    def get_settings():
        return settings.read_settings(root)

    @app.put("/api/settings")
    def put_settings(body: dict):
        try:
            return settings.update_settings(root, body)
        except ConfigError as exc:
            raise HTTPException(400, str(exc))

    # ── WS 进度（M5.2：快照 + 事件总线实时下发）────────────────────────────
    @app.websocket("/ws/progress")
    async def ws_progress(websocket: WebSocket):
        await websocket.accept()
        subscriber = bus.subscribe()
        try:
            await websocket.send_json({"type": "snapshot", "tasks": [
                {**task.to_dict(), "progress": session_progress(home / "sessions" / task.task_id)}
                for task in store.list()]})
            while True:
                try:
                    event = await asyncio.to_thread(subscriber.get, timeout=0.5)
                except queue.Empty:
                    continue
                await websocket.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            # 客户端断开 / 服务关闭（uvicorn 收尾时默认 executor 已 shutdown，
            # to_thread 抛 RuntimeError——真服务器 E2E 实测，TestClient 不走此路径）
            pass
        finally:
            bus.unsubscribe(subscriber)

    # ── 静态产物服务（关键帧/样片/角色表预览）────────────────────────────────
    app.mount("/files/sessions", StaticFiles(directory=home / "sessions"), name="sessions")
    app.mount("/files/chars", StaticFiles(directory=home / "chars"), name="chars")

    # ── SPA 静态服务（M5.4）：注册在全部 API/文件/WS 路由之后，真实文件直出，
    # 客户端路由（/tasks/<id> 等）回退 index.html；api/files/ws 前缀不吞 404。
    if web_dist is not None:
        dist = Path(web_dist).resolve()
        if (dist / "index.html").is_file():
            @app.get("/{full_path:path}", include_in_schema=False)
            async def spa(full_path: str):
                if full_path.startswith(("api/", "files/", "ws/")):
                    raise HTTPException(404, "not found")
                candidate = (dist / full_path).resolve()
                if full_path and candidate.is_file() and candidate.is_relative_to(dist):
                    return FileResponse(candidate)
                return FileResponse(dist / "index.html")

    return app