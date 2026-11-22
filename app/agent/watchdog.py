"""Agent 看门狗 + 会话恢复（M4.4）：检测 agent 半途收工不交结果，凭 MCP status 续跑。

需求六工程约束 3：模型可能半途收工不交结果（原项目 8 次运行出现 3 次），外层须检测并
续跑（先调 `status` 恢复现场，不重做已完成步骤）。恢复之所以成立，靠 MCP 侧三件持久化：

- 会话状态落盘 `$REMAKE_HOME/sessions/<sid>/case/`（meta/plan/where/paint/prompts/takes），
  agent 进程被 kill / 超时 / 自行收工后这些已完成步骤的产物仍在；
- `new_session` 幂等：会话已存在则返回 "exists; status() shows where it stands"，不重置；
- `status` 报告完整现场（ingest 与否 / 已绘关键帧 / 各窗 prompt 与 takes / 队列 queued-running-
  done-failed-orphaned / 存活 worker / image calls / final / result.json），工具描述明确
  "call it first when you resume"；队列 `.run/.done/.err` 跨进程存活，孤儿 job 由存活 worker 回队。

看门狗 = 有界重试循环：驱动一轮 agent → 检查 result.json 是否交付 → 未交付则换**恢复提示**
再驱动（恢复提示令 agent 先调 status、从已完成处续跑、不重做 ingest/重绘已绘关键帧/重渲已落位
take），交付即停。`drive` / `delivered` 可注入：单测以假驱动模拟 kill / 超时 / 半途收工各路径
（零费），E2E 以真实 Hermes × MCP × fake_llm 验证"重启续跑且不重做已完成步骤"。

与 M3.3 `run_edit_session`（单轮一次性驱动）的关系：本模块是其外层守护，单轮驱动原语不变
（M3.3 回归同构）；M5 agent 进程池以本模块管理每任务 agent 子进程的生命周期与续跑。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.agent.run import library_notes, run_edit_session
from scripts.config import resolve_remake_home

# result.json 的终态 status（save_result 落位）：任一终态都算 agent 已给出交付裁决，
# 看门狗不再续跑（partial/failed 是 agent 的明确结论，续跑无益；done 为成功交付）。
TERMINAL_STATUS = ("done", "partial", "failed")


def result_status(session_dir: Path) -> str | None:
    """会话 result.json 的 status；不存在 / 无法解析 / 无 status 字段时 None（= 未交付）。"""
    path = Path(session_dir) / "result.json"
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    status = result.get("status") if isinstance(result, dict) else None
    return status if isinstance(status, str) else None


def session_delivered(session_dir: Path) -> bool:
    """agent 是否已交付：result.json 含终态 status（done/partial/failed）。

    半途收工 / 被 kill / 超时的会话没有 result.json（或 status 非终态）→ False → 看门狗续跑。
    """
    return result_status(session_dir) in TERMINAL_STATUS


def resume_task_prompt(
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
    """会话恢复提示：令 agent 先调 status 恢复现场，不重做已完成步骤，续跑到交付。

    与 edit_task_prompt 同口径给 sid/kind/源片段/span/cast/edit/预算，但任务动词换成
    "resume"：明确要求先 status、把盘上已有的 case/关键帧/prompts/takes 视为已完成、
    只补未完成处（审查当前 take → 修仍错处 → finish → save_result）。
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
    lines.append(
        "This session was interrupted before it delivered result.json. Resume it: call status(sid) "
        "FIRST to recover where it stands. The case files, painted keyframes, prompts and rendered "
        "takes already on disk are done - do NOT redo completed steps (no re-ingest, no repaint of "
        "already-painted keyframes, no re-submit of windows whose takes already landed). Continue "
        "from where status shows: review the current takes (strip), fix only what is still wrong, "
        "then finish and save_result.")
    lines.append("Run unattended: decide, do, and deliver result.json.")
    return "\n".join(lines)


@dataclass
class WatchdogResult:
    """看门狗运行结果：轮数 / 是否交付 / 会话目录 / 各轮提示与进程结果。"""

    rounds: int
    delivered: bool
    session_dir: Path
    prompts: list[str] = field(default_factory=list)
    results: list[subprocess.CompletedProcess | None] = field(default_factory=list)

    @property
    def last_completed(self) -> subprocess.CompletedProcess | None:
        return self.results[-1] if self.results else None


def run_with_watchdog(
    root: Path,
    sid: str,
    prompt: str,
    *,
    base_url: str,
    api_key: str,
    resume_prompt: str,
    home: Path | None = None,
    platform: str | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: float = 600.0,
    max_rounds: int = 3,
    verbose: bool = False,
    drive: Callable[[str], tuple[subprocess.CompletedProcess, Path, Path]] | None = None,
    delivered: Callable[[Path], bool] = session_delivered,
    log: Callable[[str], None] = print,
) -> WatchdogResult:
    """看门狗守护一次翻拍会话：驱动 → 检查交付 → 未交付凭 status 恢复续跑（有界 max_rounds）。

    第 1 轮用初始 prompt；其后每轮用 resume_prompt（先调 status、不重做已完成步骤）。
    任一轮交付（delivered(session_dir) 为真）即停。agent 进程超时（subprocess.TimeoutExpired）
    视为半途终止，同样续跑。drive(prompt) → (进程结果, 工作目录, 会话目录)，缺省即
    run_edit_session（单轮原语）；注入假 drive 可零费模拟 kill / 超时 / 半途收工。

    → WatchdogResult（rounds=实际轮数，delivered=是否交付，prompts/results 各轮轨迹）。
    max_rounds 耗尽仍未交付时 delivered=False（交由上层裁决，不无限重试）。
    """
    if max_rounds < 1:
        raise ValueError(f"max_rounds 须 >= 1，实际 {max_rounds}")
    effective_home = home or resolve_remake_home(root)
    session_dir = effective_home / "sessions" / sid

    def _default_drive(current: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
        return run_edit_session(
            root, sid, current, base_url=base_url, api_key=api_key, home=effective_home,
            platform=platform, extra_env=extra_env, timeout=timeout, verbose=verbose, log=log)

    drive = drive or _default_drive
    prompts: list[str] = []
    results: list[subprocess.CompletedProcess | None] = []
    current = prompt
    for round_index in range(1, max_rounds + 1):
        prompts.append(current)
        log(f"[watchdog] 第 {round_index}/{max_rounds} 轮驱动 agent（sid={sid}）。")
        try:
            completed, _workspace, session_dir = drive(current)
        except subprocess.TimeoutExpired:
            log(f"[watchdog] 第 {round_index} 轮 agent 超时（{timeout:g}s）被终止。")
            completed = None
        results.append(completed)
        if delivered(session_dir):
            log(f"[watchdog] 第 {round_index} 轮已交付 result.json（status={result_status(session_dir)}）。")
            return WatchdogResult(round_index, True, session_dir, prompts, results)
        if round_index < max_rounds:
            log(f"[watchdog] 第 {round_index} 轮未交付 result.json（半途收工/被 kill/超时）；凭 status 恢复续跑。")
            current = resume_prompt
    log(f"[watchdog] {max_rounds} 轮耗尽仍未交付 result.json；停止续跑，交上层裁决。")
    return WatchdogResult(max_rounds, False, session_dir, prompts, results)