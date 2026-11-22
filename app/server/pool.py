"""Agent 进程池 + 事件总线（M5.2）：领取 created/interrupted 任务，看门狗驱动，日志采集。

需求八：每任务一个 agent 子进程，生命周期管理与日志采集；进度读 sessions/ 产物 +
事件 WebSocket 实时下发。分工：

- 领取：dispatcher 线程轮询任务表，created/interrupted 且未在册的任务拨 running 并起
  任务线程（并发上限 max_workers，渲染排队由 MCP 队列承载，agent 并发只受 LLM 预算约束）；
- 驱动：每任务线程跑 M4.4 run_with_watchdog（有界续跑原语）。提示按任务态选择——
  created 首轮 task_prompt / created 反馈轮 followup_prompt（rounds>1）/ interrupted
  恢复轮 resume_task_prompt（重启续跑，先 status 不重做已完成步骤）；
- 裁决：交付 → 读 result.json 同步终态与成片路径；max_rounds 耗尽仍未交付 → failed
  （M4.4 留下的上层裁决：终态可反馈重制再开一轮，不自动重领以免无限循环）；驱动异常
  同样 failed 并附错误事件；
- 日志：watchdog 轨迹 + 各轮 agent stdout/stderr 追加 logs/<task_id>.log，log 事件截断下发；
- 进度：ticker 线程按周期对在册任务读 session_progress 发 progress 事件（产物增长实时可见），
  状态变迁/轮次起止另发 task_status/round 事件；WS 端点（api.py）订阅总线转发。

drive(task, prompt) 可注入：缺省 run_edit_session（config.toml [llm] 三元组），单测注入
进程内假驱动、E2E 注入真实 Hermes × MCP 单轮驱动（M5.4）——与 M2.4 supervise 同范式。
注意注入契约是**单轮** drive：返回 (进程结果, 工作目录, 会话目录) 三元组，看门狗由
本池 _run_task 自带（run_with_watchdog 包裹），注入方不得再包看门狗/返回 WatchdogResult。
timeout 缺省读 config.toml [agent].round_timeout（7200s，真渲染会话小时级；旧缺省 600s
会误杀任何平台的真实渲染轮）。
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from pathlib import Path
from typing import Callable

from app.agent.watchdog import result_status, resume_task_prompt, run_with_watchdog
from app.server.prompts import followup_prompt, session_progress, task_prompt
from app.server.store import Task, TaskStore
from scripts.config import load_config

# 单条 log 事件下发的最大字符数（全量在 logs/<task_id>.log，API 可取）
LOG_EVENT_LIMIT = 4000


class EventBus:
    """线程安全事件总线：订阅者各得一个队列，publish 广播（WS 端点逐订阅者转发）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []

    def subscribe(self) -> queue.Queue:
        subscriber: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def publish(self, event: dict) -> None:
        with self._lock:
            for subscriber in self._subscribers:
                subscriber.put_nowait(event)


def prompt_for(task: Task, *, status: str | None = None, chars_dir: Path | None = None) -> str:
    """按任务态选驱动提示：created 首轮/反馈轮、interrupted 恢复轮（M4.4 口径）。

    status 传入领取前的状态（dispatcher 已把任务拨 running，提示选择须凭原态）。
    chars_dir 给出时首轮/恢复轮附角色库落位清单（反馈轮沿用已有 sheet，不附）。
    """
    if (status or task.status) == "interrupted":
        return resume_task_prompt(task.task_id, task.src, kind=task.mode, start=task.start_s,
                                  dur=task.dur_s, edit=task.description if task.mode == "edit" else None,
                                  cast=task.cast or None, budget=task.budget, chars_dir=chars_dir)
    if task.rounds > 1:
        return followup_prompt(task)
    return task_prompt(task, chars_dir=chars_dir)


class AgentPool:
    """agent 进程池：dispatcher 领取 + 每任务一个看门狗线程 + ticker 进度事件。

    drive(task, prompt) → (进程结果, 工作目录, 会话目录)，缺省走 run_edit_session
    （真实 Hermes × MCP）；注入假驱动零费测试。start/stop 管理线程生命周期。
    """

    def __init__(self, root: Path, home: Path, store: TaskStore, bus: EventBus, *,
                 drive: Callable[[Task, str], tuple[subprocess.CompletedProcess, Path, Path]] | None = None,
                 max_workers: int = 1, max_rounds: int = 3, timeout: float | None = None,
                 poll_interval: float = 0.2, ticker_interval: float = 2.0,
                 log: Callable[[str], None] = print) -> None:
        if max_workers < 1:
            raise ValueError(f"max_workers 须 >= 1，实际 {max_workers}")
        self.root, self.home, self.store, self.bus = Path(root), Path(home), store, bus
        self.drive = drive
        self.max_workers, self.max_rounds = max_workers, max_rounds
        self.poll_interval, self.ticker_interval = poll_interval, ticker_interval
        self.log = log
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._llm = None if drive is not None else load_config(self.root)
        # 单轮超时缺省读 config [agent].round_timeout（显式注入优先，测试用短超时）
        self.timeout = float(timeout if timeout is not None
                             else (self._llm or load_config(self.root)).agent_round_timeout)

    # ── 生命周期 ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        self._stop.clear()
        for target, name in ((self._dispatch, "pool-dispatch"), (self._tick, "pool-ticker")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 10.0) -> None:
        """停止领取与进度推送，等任务线程收尾（在途 agent 子进程随轮自然结束）。"""
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()

    def _dispatch(self) -> None:
        while not self._stop.is_set():
            try:
                self._pick_once()
            except Exception as exc:  # 单任务异常不杀 dispatcher
                self.log(f"[pool] 领取异常：{type(exc).__name__}: {exc}")
            self._stop.wait(self.poll_interval)

    def _pick_once(self) -> None:
        for task in self.store.list():
            if task.status not in ("created", "interrupted"):
                continue
            with self._lock:
                if task.task_id in self._inflight or len(self._inflight) >= self.max_workers:
                    continue
                self._inflight.add(task.task_id)
            self.store.set_status(task.task_id, "running")
            thread = threading.Thread(target=self._run_task, args=(task.task_id, task.status),
                                      name=f"agent-{task.task_id}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def _tick(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                inflight = sorted(self._inflight)
            for task_id in inflight:
                self.bus.publish({"type": "progress", "task_id": task_id,
                                  "progress": session_progress(self.home / "sessions" / task_id)})
            self._stop.wait(self.ticker_interval)

    # ── 每任务驱动 ───────────────────────────────────────────────────────────
    def _run_task(self, task_id: str, picked_status: str) -> None:
        session_dir = self.home / "sessions" / task_id
        try:
            task = self.store.get(task_id)
            chars_dir = self.home / "chars"
            prompt = prompt_for(task, status=picked_status, chars_dir=chars_dir)
            resume = resume_task_prompt(task.task_id, task.src, kind=task.mode, start=task.start_s,
                                        dur=task.dur_s, edit=task.description if task.mode == "edit" else None,
                                        cast=task.cast or None, budget=task.budget, chars_dir=chars_dir)
            self.bus.publish({"type": "task_status", "task_id": task_id, "status": "running",
                              "rounds": task.rounds})
            self._log_line(task_id, f"[pool] 领取任务（rounds={task.rounds}，提示 {len(prompt)} 字符）。")
            result = run_with_watchdog(
                self.root, task_id, prompt, base_url=self._llm.llm_base_url if self._llm else "",
                api_key=self._llm.llm_api_key if self._llm else "", resume_prompt=resume,
                home=self.home, timeout=self.timeout, max_rounds=self.max_rounds,
                drive=self._round_drive(task_id), log=lambda message: self._log_line(task_id, message))
            for index, completed in enumerate(result.results, start=1):
                if completed is not None:
                    self._collect(task_id, index, completed)
            if result.delivered:
                status = result_status(session_dir) or "done"
                final = ""
                try:
                    final = str(json.loads((session_dir / "result.json").read_text(encoding="utf-8"))
                                .get("final") or "")
                except (OSError, ValueError):
                    pass
                self.store.set_status(task_id, status, final=final)
                self.bus.publish({"type": "task_status", "task_id": task_id, "status": status,
                                  "rounds": task.rounds, "final": final,
                                  "watchdog_rounds": result.rounds})
                self._log_line(task_id, f"[pool] 交付（status={status}，看门狗 {result.rounds} 轮）。")
            else:
                # max_rounds 耗尽的上层裁决（M4.4 遗留）：failed 终态，可反馈重制再开一轮
                self.store.set_status(task_id, "failed")
                self.bus.publish({"type": "task_status", "task_id": task_id, "status": "failed",
                                  "rounds": task.rounds, "reason": "watchdog_exhausted",
                                  "watchdog_rounds": result.rounds})
                self._log_line(task_id, f"[pool] {result.rounds} 轮耗尽未交付，裁决 failed（可反馈重制）。")
        except Exception as exc:
            self.store.set_status(task_id, "failed")
            self.bus.publish({"type": "task_status", "task_id": task_id, "status": "failed",
                              "reason": f"{type(exc).__name__}: {exc}"})
            self._log_line(task_id, f"[pool] 驱动异常：{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._inflight.discard(task_id)

    def _round_drive(self, task_id: str):
        """看门狗单轮 drive：注入驱动按任务绑定；缺省即 run_edit_session（watchdog 内建）。"""
        if self.drive is None:
            return None
        task = self.store.get(task_id)

        def drive(prompt: str):
            self.bus.publish({"type": "round_start", "task_id": task_id, "prompt_chars": len(prompt)})
            completed, workspace, session_dir = self.drive(task, prompt)
            self.bus.publish({"type": "round_end", "task_id": task_id,
                              "returncode": completed.returncode})
            return completed, workspace, session_dir

        return drive

    # ── 日志采集 ─────────────────────────────────────────────────────────────
    def _log_path(self, task_id: str) -> Path:
        path = self.home / "logs" / f"{task_id}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _log_line(self, task_id: str, message: str) -> None:
        self.log(message)
        with self._log_path(task_id).open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")
        self.bus.publish({"type": "log", "task_id": task_id, "line": message[:LOG_EVENT_LIMIT]})

    def _collect(self, task_id: str, round_index: int, completed: subprocess.CompletedProcess) -> None:
        text = f"[agent 第 {round_index} 轮 rc={completed.returncode}]\n{completed.stdout or ''}{completed.stderr or ''}"
        with self._log_path(task_id).open("a", encoding="utf-8") as handle:
            handle.write(text)
        self.bus.publish({"type": "log", "task_id": task_id,
                          "line": text[:LOG_EVENT_LIMIT]})