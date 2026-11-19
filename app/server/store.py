"""任务持久化（M5.1）：sqlite 任务表——task_id ↔ MCP session id、状态、产物路径。

需求八：重启后可恢复任务列表与进度。task_id 即 MCP session id（1:1，uploads/<task_id>/
与 sessions/<task_id>/ 同名落位）；状态机 created → running → done/partial/failed，
反馈重制把终态任务拨回 created（rounds+1，复用原 session，5.2 进程池凭 follow-up 提示
下发新一轮）。running 中的任务重启后由 recover() 对盘上 result.json 重新裁决：已交付
则同步终态，未交付标 interrupted（看门狗续跑的输入，M4.4）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# 任务状态：created 待驱动 / running agent 在跑 / done partial failed 交付终态（MCP
# save_result 同名）/ interrupted 重启时 running 但未交付（待看门狗续跑）。
STATUSES = ("created", "running", "done", "partial", "failed", "interrupted")
TERMINAL = ("done", "partial", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    description TEXT NOT NULL,
    cast TEXT NOT NULL DEFAULT '{}',
    budget INTEGER,
    start_s REAL NOT NULL DEFAULT 0,
    dur_s REAL,
    src TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    rounds INTEGER NOT NULL DEFAULT 1,
    feedback TEXT NOT NULL DEFAULT '[]',
    final TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""


@dataclass
class Task:
    """任务表一行（cast/feedback 反序列化为 dict/list）。"""

    task_id: str
    mode: str
    description: str
    src: str
    cast: dict = field(default_factory=dict)
    budget: int | None = None
    start_s: float = 0.0
    dur_s: float | None = None
    status: str = "created"
    rounds: int = 1
    feedback: list = field(default_factory=list)
    final: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "mode": self.mode, "description": self.description,
            "cast": self.cast, "budget": self.budget, "start": self.start_s, "dur": self.dur_s,
            "src": self.src, "status": self.status, "rounds": self.rounds,
            "feedback": self.feedback, "final": self.final,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        task_id=row["task_id"], mode=row["mode"], description=row["description"],
        src=row["src"], cast=json.loads(row["cast"]), budget=row["budget"],
        start_s=row["start_s"], dur_s=row["dur_s"], status=row["status"],
        rounds=row["rounds"], feedback=json.loads(row["feedback"]), final=row["final"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


class TaskStore:
    """sqlite 任务表（$REMAKE_HOME/tasks.db）。check_same_thread=False + 锁：
    FastAPI 线程池与 TestClient 跨线程共用一连接。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create(self, task: Task) -> Task:
        now = time.time()
        task.created_at = task.updated_at = now
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (task_id, mode, description, cast, budget, start_s, dur_s,"
                " src, status, rounds, feedback, final, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task.task_id, task.mode, task.description, json.dumps(task.cast, ensure_ascii=False),
                 task.budget, task.start_s, task.dur_s, task.src, task.status, task.rounds,
                 json.dumps(task.feedback, ensure_ascii=False), task.final, now, now))
            self._conn.commit()
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def list(self) -> list[Task]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
        return [_row_to_task(row) for row in rows]

    def _update(self, task_id: str, **fields) -> Task:
        if "cast" in fields:
            fields["cast"] = json.dumps(fields["cast"], ensure_ascii=False)
        if "feedback" in fields:
            fields["feedback"] = json.dumps(fields["feedback"], ensure_ascii=False)
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self._conn.execute(f"UPDATE tasks SET {sets} WHERE task_id = ?",
                               (*fields.values(), task_id))
            self._conn.commit()
        task = self.get(task_id)
        assert task is not None, task_id
        return task

    def set_status(self, task_id: str, status: str, *, final: str | None = None) -> Task:
        assert status in STATUSES, f"未知任务状态 {status!r}"
        fields = {"status": status}
        if final is not None:
            fields["final"] = final
        return self._update(task_id, **fields)

    def add_feedback(self, task_id: str, entry: dict) -> Task:
        """反馈重制：终态任务追加反馈、rounds+1、拨回 created（复用原 session 待再驱动）。"""
        task = self.get(task_id)
        assert task is not None, task_id
        if task.status not in TERMINAL:
            raise ValueError(f"任务 {task_id} 状态 {task.status} 非终态，不能反馈重制")
        return self._update(task_id, feedback=task.feedback + [entry],
                            rounds=task.rounds + 1, status="created")

    def recover(self, session_dir_of) -> list[Task]:
        """重启恢复：running 任务对盘上 result.json 重新裁决（session_dir_of(task_id)
        → 会话目录）。已交付同步终态与成片路径；未交付标 interrupted 待看门狗续跑。"""
        changed = []
        for task in self.list():
            if task.status != "running":
                continue
            result_path = Path(session_dir_of(task.task_id)) / "result.json"
            status, final = None, ""
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
                status = result.get("status")
                final = str(result.get("final") or "")
            except (OSError, ValueError):
                pass
            if status in TERMINAL:
                changed.append(self.set_status(task.task_id, status, final=final))
            else:
                changed.append(self.set_status(task.task_id, "interrupted"))
        return changed