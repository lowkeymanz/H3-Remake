"""驱动提示与进度推导（M5.1 抽出，M5.2 进程池复用）：任务表 → 提示口径 + 会话产物 → 阶段进度。

api.py 与 pool.py 共用（pool 不依赖 FastAPI 应用层，避免循环导入）；口径见 M5.1 笔记：
task_prompt 走 M3.3 任务口径，followup_prompt 走 AGENTS.md follow-up round（只重跑点名
窗口），session_progress 读 sessions/<sid>/ 产物推导阶段时间线。
"""

from __future__ import annotations

from pathlib import Path

from app.agent.run import edit_task_prompt
from app.agent.watchdog import result_status
from app.server.store import Task


def task_prompt(task: Task, *, chars_dir: Path | None = None) -> str:
    """任务表 → 驱动提示（M3.3 任务口径：edit 传句子、swap 传 cast）。

    chars_dir 给出时 swap 提示附角色库落位清单，防 agent 自造库中已有角色。
    """
    return edit_task_prompt(task.task_id, task.src, kind=task.mode, start=task.start_s,
                            dur=task.dur_s, edit=task.description if task.mode == "edit" else None,
                            cast=task.cast or None, budget=task.budget, chars_dir=chars_dir)


def followup_prompt(task: Task) -> str:
    """反馈重制提示（AGENTS.md：用户反馈开启新一轮，只重跑点名的窗口）。

    复用原 session：先 status 恢复现场，不重做已完成步骤；反馈点名的窗口走修复轮
    （重绘/换 seed/钉切点按审查结论），其余窗口沿用已交付的 take，收尾重新交付。
    """
    entry = task.feedback[-1]
    span = (f"[{task.start_s:g}, {task.start_s + task.dur_s:g})"
            if task.dur_s is not None else "the whole clip")
    lines = [f"Session id: {task.task_id}. Kind: {task.mode}.",
             f"Source clip: {task.src}. Span: {span} (seconds).",
             f"This is round {task.rounds}: the user reviewed the delivered video and gave feedback.",
             f"User feedback: {entry['text']}"]
    if entry.get("windows"):
        lines.append("Windows the feedback names: " + ", ".join(f"w{w}" for w in entry["windows"]))
    lines += ["Call status(sid) FIRST to recover where the session stands. The case files, painted "
              "keyframes and delivered takes on disk are done - do NOT redo completed steps (no "
              "re-ingest, no repaint of already-painted keyframes). Open a fix round only for the "
              "windows the feedback names; keep the delivered takes of every other window. Then "
              "finish and save_result again.",
              "Run the whole job now, unattended: decide, do, and deliver result.json."]
    return "\n".join(lines)


def session_progress(session_dir: Path) -> dict:
    """读会话产物推导阶段进度（需求八：读 sessions/ 产物；事件推送见 pool.py）。"""
    session_dir = Path(session_dir)
    case = session_dir / "case"
    painted = sum(1 for p in case.glob("paint/*/f*.png") if p.parent.name != "src")
    return {
        "ingest": (case / "meta.json").is_file(),
        "plan": (case / "plan_grid.json").is_file(),
        "painted": painted,
        "takes": len(list((case / "takes").glob("*.mp4"))) if (case / "takes").is_dir() else 0,
        "review": any(case.glob("look/strips_*.jpg")),
        "final": any(case.glob("final/full_*.mp4")),
        "result": result_status(session_dir),
    }