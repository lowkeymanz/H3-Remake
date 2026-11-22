"""Agent 会话编排（M3.3）：把一次视频翻拍任务组装成 Hermes 驱动命令并跑到收尾。

分工：任务提示 = 原项目 AGENTS.md 的任务口径（sid、kind、源片段、span、edit/cast、
预算）；Hermes 由 hermes_command 定位运行时并构造 `python -m hermes_cli.main`；
工作目录由 prepare_workspace 落位 AGENTS.md（M3.2）。本模块只做"任务进、命令出、
进程跑完"的编排——假件（fake_llm / fake_painter / fake_comfy_worker）与真实件
（qwen3.7-plus / MiniMax / comfy_worker）对它同构，替换即升级（M4 渐进接真件）。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable, Sequence

from app.agent.hermes import hermes_command
from app.agent.workspace import prepare_workspace
from scripts.config import resolve_remake_home


def library_notes(cast: dict[str, str], chars_dir: Path) -> list[str]:
    """cast 目标在角色库的落位清单：逐角色点名 sheet(source="lib") 与库描述。

    只给 cast 行时弱模型会照源人物外观自造角色（sheet(source="text")），替换形同未换；
    库信息随提示直达，不依赖 agent 自行发现 $REMAKE_HOME/chars。
    """
    chars_dir = Path(chars_dir)
    lib: dict = {}
    for path in sorted(chars_dir.glob("lib_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            lib.setdefault("chars", {}).update(data.get("chars") or {})
            lib.setdefault("outfits", {}).update(data.get("outfits") or {})
    notes = []
    for name in dict.fromkeys(cast.values()):
        if not (chars_dir / f"{name}_sheet.png").is_file():
            continue
        photo = ", photo" if (chars_dir / f"{name}_photo.png").is_file() else ""
        note = (f"Character library: {name} is in the operator's library "
                f"($REMAKE_HOME/chars/{name}_sheet.png{photo}). Use sheet(source=\"lib\") for {name}; "
                f"do NOT invent or redraw this character.")
        desc, outfit = lib.get("chars", {}).get(name), lib.get("outfits", {}).get(name)
        if desc:
            note += f" Library description: {desc}"
        if outfit:
            note += f" Outfit: {outfit}"
        notes.append(note)
    return notes


def edit_task_prompt(
    sid: str,
    src: str,
    *,
    kind: str = "edit",
    start: float = 0.0,
    dur: float | None = None,
    edit: str | None = None,
    cast: dict[str, str] | None = None,
    budget: int | None = None,
    chars_dir: Path | None = None,
) -> str:
    """按原项目任务口径组装驱动提示（AGENTS.md：任务给 sid、kind、源片段、cast/edit、预算）。

    edit 会话传 edit 句子；swap 会话传 cast 映射（源人 id -> 替换角色）。budget 缺省
    不写（用 MCP 侧 REMAKE_MAX_IMAGES 默认）。chars_dir 给出时附角色库落位清单。
    """
    span = f"[{start:g}, {start + dur:g})" if dur is not None else "the whole clip"
    lines = [f"Session id: {sid}. Kind: {kind}.",
             f"Source clip: {src}. Span: {span} (seconds)."]
    if edit is not None:
        lines.append(f"Edit: {edit}")
    if cast is not None:
        lines.append("Cast: " + ", ".join(f"{who} -> {whom}" for who, whom in cast.items()))
        if chars_dir is not None:
            lines.extend(library_notes(cast, chars_dir))
    if budget is not None:
        lines.append(f"Image-call budget for this session: {budget}.")
    lines.append("Run the whole job now, unattended: decide, do, and deliver result.json.")
    return "\n".join(lines)


def hermes_chat_command(
    root: Path,
    prompt: str,
    workspace: Path,
    *,
    base_url: str,
    api_key: str,
    platform: str | None = None,
    home: Path | None = None,
    verbose: bool = False,
) -> tuple[list[str], dict[str, str]]:
    """Hermes 一次性任务命令：chat --oneshot --yolo --in 工作目录，LLM 指向 base_url。"""
    return hermes_command(
        root,
        ["chat", "-q", prompt, "--oneshot", "--yolo",
         *(["-v"] if verbose else []),
         "--in", str(workspace),
         "--provider", "custom",
         "--base-url", base_url,
         "--api-key", api_key],
        platform=platform,
        home=home,
    )


def run_edit_session(
    root: Path,
    sid: str,
    prompt: str,
    *,
    base_url: str,
    api_key: str,
    home: Path | None = None,
    platform: str | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: float = 600.0,
    verbose: bool = False,
    log: Callable[[str], None] = print,
) -> tuple[subprocess.CompletedProcess, Path, Path]:
    """跑完一次翻拍会话：工作目录落位 → Hermes 一次性驱动 → 等待收尾。

    → (进程结果, 工作目录, 会话目录)。extra_env 注入假件/真实件所需环境
    （OPENAI_BASE_URL 指假 painter、PATH 前置 ffmpeg 等）；命令本身的
    HERMES_HOME 环境键由 hermes_command 给出，与其合并。
    """
    effective_home = home or resolve_remake_home(root)
    workspace = prepare_workspace(root, sid, log=log)
    command, env = hermes_chat_command(root, prompt, workspace,
                                        base_url=base_url, api_key=api_key, platform=platform,
                                        home=effective_home, verbose=verbose)
    run_env = {**os.environ, **env, **(extra_env or {})}
    log(f"[agent] 驱动 Hermes：{' '.join(command[:2])} ... --in {workspace}")
    completed = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                env=run_env, timeout=timeout)
    session_dir = effective_home / "sessions" / sid
    return completed, workspace, session_dir
