"""M5.2：agent 进程池 + WS 进度——零费单测 + fake agent 子进程 E2E。

零 GPU / 零 API 费：
- 单测：EventBus 订阅/广播/退订、prompt_for 三态提示选择、AgentPool 注入进程内假驱动
  （领取交付/看门狗续跑/耗尽裁决 failed/驱动异常/反馈轮自动再领取/interrupted 恢复提示/
  并发上限/日志采集/ticker 进度事件）、WS 快照与事件下发、日志端点；
- E2E：以 scripts/fake_agent.py 为真实子进程（需求 5.2 独立测试口径），经 API + 进程池 +
  WS 全链路——上传→自动领取→进度事件→交付下载；半途收工看门狗凭恢复提示续跑；耗尽
  failed 后反馈重制恢复；重启 interrupted 凭恢复提示续跑。
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agent.watchdog import WatchdogResult
from app.server.api import create_app
from app.server.pool import AgentPool, EventBus, prompt_for
from app.server.store import Task, TaskStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def wait_for(predicate, timeout: float = 15.0, message: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"等待超时：{message}")


def drain(subscriber: queue.Queue) -> list[dict]:
    events = []
    while True:
        try:
            events.append(subscriber.get_nowait())
        except queue.Empty:
            return events


def _task(task_id: str, **kw) -> Task:
    defaults = dict(mode="edit", description="remove the ball", src="D:/clips/a.mp4")
    defaults.update(kw)
    return Task(task_id=task_id, **defaults)


# ── EventBus 单测 ────────────────────────────────────────────────────────────
class EventBusTests(unittest.TestCase):
    """订阅/广播/退订：每订阅者独立队列，publish 线程安全广播。"""

    def test_publish_reaches_all_subscribers(self) -> None:
        bus = EventBus()
        first, second = bus.subscribe(), bus.subscribe()
        bus.publish({"type": "x", "n": 1})
        self.assertEqual(first.get_nowait(), {"type": "x", "n": 1})
        self.assertEqual(second.get_nowait(), {"type": "x", "n": 1})

    def test_unsubscribe_stops_delivery(self) -> None:
        bus = EventBus()
        subscriber = bus.subscribe()
        bus.unsubscribe(subscriber)
        bus.publish({"type": "x"})
        self.assertTrue(subscriber.empty())
        bus.unsubscribe(subscriber)  # 重复退订不报错

    def test_no_subscribers_publish_safe(self) -> None:
        EventBus().publish({"type": "x"})


# ── prompt_for 单测 ──────────────────────────────────────────────────────────
class PromptForTests(unittest.TestCase):
    """三态提示选择：created 首轮 / created 反馈轮 / interrupted 恢复轮。"""

    def test_created_first_round_uses_task_prompt(self) -> None:
        prompt = prompt_for(_task("t1"))
        self.assertIn("Session id: t1. Kind: edit.", prompt)
        self.assertIn("Edit: remove the ball", prompt)
        self.assertNotIn("do NOT redo completed steps", prompt)

    def test_created_feedback_round_uses_followup(self) -> None:
        task = _task("t1", rounds=2, feedback=[{"text": "cut feels early", "windows": [0]}])
        prompt = prompt_for(task)
        self.assertIn("User feedback: cut feels early", prompt)
        self.assertIn("round 2", prompt)

    def test_interrupted_uses_resume_prompt(self) -> None:
        task = _task("t1", status="interrupted")
        prompt = prompt_for(task)
        self.assertIn("This session was interrupted", prompt)
        self.assertIn("status(sid) FIRST", prompt)
        self.assertIn("do NOT redo completed steps", prompt)

    def test_status_override_wins_over_running(self) -> None:
        task = _task("t1", status="running")  # dispatcher 已拨 running
        self.assertIn("This session was interrupted", prompt_for(task, status="interrupted"))
        self.assertNotIn("This session was interrupted", prompt_for(task, status="created"))

    def test_chars_dir_wires_library_notes_into_first_and_resume(self) -> None:
        """首轮/恢复轮附角色库落位清单；反馈轮沿用已有 sheet 不附。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            chars = Path(temporary)
            (chars / "Vela_sheet.png").write_bytes(b"sheet")
            task = _task("t1", mode="swap", cast={"P1": "Vela"})
            self.assertIn('sheet(source="lib") for Vela', prompt_for(task, chars_dir=chars))
            self.assertIn('sheet(source="lib") for Vela',
                          prompt_for(task, status="interrupted", chars_dir=chars))
            feedback = _task("t1", mode="swap", cast={"P1": "Vela"}, rounds=2,
                             feedback=[{"text": "again", "windows": [0]}])
            self.assertNotIn("Character library:", prompt_for(feedback, chars_dir=chars))


# ── AgentPool 单测（注入进程内假驱动，零子进程）─────────────────────────────
class PoolUnitTests(unittest.TestCase):
    """领取/驱动/裁决/日志/进度：drive 注入按计划落 result.json 或模拟失败。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        (self.home / "sessions").mkdir(parents=True)
        self.store = TaskStore(self.home / "tasks.db")
        self.bus = EventBus()
        self.plans: dict[str, deque] = {}
        self.prompts: dict[str, list[str]] = {}
        self.pool = self._pool()

    def tearDown(self) -> None:
        self.pool.stop()
        self.store.close()
        self._tmp.cleanup()

    def _pool(self, **kw) -> AgentPool:
        params = dict(drive=self._drive, max_workers=1, max_rounds=3,
                      poll_interval=0.05, ticker_interval=0.05, log=lambda _m: None)
        params.update(kw)
        return AgentPool(self.root, self.home, self.store, self.bus, **params)

    def _drive(self, task: Task, prompt: str):
        self.prompts.setdefault(task.task_id, []).append(prompt)
        plans = self.plans.get(task.task_id)
        plan = plans.popleft() if plans else {"deliver": True}
        if plan.get("block") is not None:
            plan["block"].wait(10)
        if plan.get("raise"):
            raise RuntimeError(plan["raise"])
        session = self.home / "sessions" / task.task_id
        session.mkdir(parents=True, exist_ok=True)
        if plan.get("deliver"):
            final = session / "case" / "final" / "full_480.mp4"
            final.parent.mkdir(parents=True, exist_ok=True)
            final.write_bytes(b"fake-final-video")
            (session / "result.json").write_text(
                json.dumps({"status": "done", "final": str(final)}), encoding="utf-8")
        return (subprocess.CompletedProcess([], plan.get("rc", 0), stdout="fake stdout", stderr=""),
                self.home, session)

    def _create(self, task_id: str, **kw) -> Task:
        return self.store.create(_task(task_id, **kw))

    def _status(self, task_id: str) -> str:
        return self.store.get(task_id).status

    def test_pickup_delivers_and_syncs_final(self) -> None:
        task = self._create("t1")
        subscriber = self.bus.subscribe()
        self.pool.start()
        wait_for(lambda: self._status("t1") == "done", message="t1 未交付")
        got = self.store.get("t1")
        self.assertTrue(got.final.endswith("full_480.mp4"))
        self.assertEqual(self.prompts["t1"], [prompt_for(task)])
        events = drain(subscriber)
        statuses = [(e["status"], e.get("watchdog_rounds")) for e in events
                    if e["type"] == "task_status" and e["task_id"] == "t1"]
        self.assertEqual(statuses, [("running", None), ("done", 1)])
        self.assertTrue(any(e["type"] == "round_end" and e["returncode"] == 0 for e in events))

    def test_watchdog_resumes_after_premature_stop(self) -> None:
        self._create("t1")
        self.plans["t1"] = deque([{"deliver": False, "rc": -15}, {"deliver": True}])
        subscriber = self.bus.subscribe()
        self.pool.start()
        wait_for(lambda: self._status("t1") == "done", message="续跑未交付")
        self.assertEqual(len(self.prompts["t1"]), 2)
        self.assertIn("do NOT redo completed steps", self.prompts["t1"][1])  # 恢复提示
        events = drain(subscriber)
        done = [e for e in events if e["type"] == "task_status" and e.get("status") == "done"]
        self.assertEqual(done[0]["watchdog_rounds"], 2)

    def test_exhausted_marks_failed_terminal(self) -> None:
        self._create("t1")
        self.plans["t1"] = deque([{"deliver": False}, {"deliver": False}])
        subscriber = self.bus.subscribe()
        self.pool = self._pool(max_rounds=2)
        self.pool.start()
        wait_for(lambda: self._status("t1") == "failed", message="耗尽未裁决 failed")
        self.assertEqual(len(self.prompts["t1"]), 2)  # 有界，不无限重试
        events = drain(subscriber)
        failed = [e for e in events if e["type"] == "task_status" and e.get("status") == "failed"]
        self.assertEqual(failed[0]["reason"], "watchdog_exhausted")
        time.sleep(0.2)  # failed 是终态：不再被领取
        self.assertEqual(self._status("t1"), "failed")
        self.assertEqual(len(self.prompts["t1"]), 2)

    def test_drive_exception_marks_failed(self) -> None:
        self._create("t1")
        self.plans["t1"] = deque([{"raise": "boom"}])
        subscriber = self.bus.subscribe()
        self.pool.start()
        wait_for(lambda: self._status("t1") == "failed", message="异常未裁决 failed")
        events = drain(subscriber)
        failed = [e for e in events if e["type"] == "task_status" and e.get("status") == "failed"]
        self.assertIn("RuntimeError: boom", failed[0]["reason"])

    def test_feedback_round_auto_repicked_with_followup(self) -> None:
        self._create("t1")
        self.pool.start()
        wait_for(lambda: self._status("t1") == "done", message="首轮未交付")
        self.plans["t1"] = deque([{"deliver": True}])
        self.store.add_feedback("t1", {"text": "cut feels early", "windows": [0]})
        wait_for(lambda: self._status("t1") == "done" and self.store.get("t1").rounds == 2,
                 message="反馈轮未交付")
        self.assertEqual(len(self.prompts["t1"]), 2)
        self.assertIn("User feedback: cut feels early", self.prompts["t1"][1])

    def test_interrupted_task_resumes_with_recovery_prompt(self) -> None:
        self._create("t1", status="interrupted")
        self.pool.start()
        wait_for(lambda: self._status("t1") == "done", message="恢复未交付")
        self.assertIn("This session was interrupted", self.prompts["t1"][0])
        self.assertIn("status(sid) FIRST", self.prompts["t1"][0])

    def test_max_workers_capacity(self) -> None:
        release = threading.Event()
        self._create("twait")
        self._create("tblock")  # 后建先领（列表最新优先）：阻塞者占住唯一容量
        self.plans["tblock"] = deque([{"deliver": True, "block": release}])
        self.pool.start()
        wait_for(lambda: self._status("tblock") == "running", message="tblock 未领取")
        time.sleep(0.2)
        self.assertEqual(self._status("twait"), "created")  # 容量 1：排队不领取
        release.set()
        wait_for(lambda: self._status("tblock") == "done" and self._status("twait") == "done",
                 message="释放后未全部交付")

    def test_log_collection_and_events(self) -> None:
        self._create("t1")
        subscriber = self.bus.subscribe()
        self.pool.start()
        wait_for(lambda: self._status("t1") == "done", message="未交付")
        log = (self.home / "logs" / "t1.log").read_text(encoding="utf-8")
        self.assertIn("[watchdog]", log)  # 看门狗轨迹
        self.assertIn("fake stdout", log)  # agent 子进程输出
        self.assertIn("[pool] 交付", log)
        events = drain(subscriber)
        self.assertTrue(any(e["type"] == "log" and "fake stdout" in e["line"] for e in events))

    def test_ticker_publishes_progress_while_running(self) -> None:
        release = threading.Event()
        self._create("t1")
        self.plans["t1"] = deque([{"deliver": True, "block": release}])
        subscriber = self.bus.subscribe()
        self.pool.start()
        wait_for(lambda: self._status("t1") == "running", message="未领取")
        wait_for(lambda: any(e["type"] == "progress" and e["task_id"] == "t1"
                             for e in drain(subscriber)), message="无进度事件")
        release.set()
        wait_for(lambda: self._status("t1") == "done", message="未交付")

    def test_max_workers_validation(self) -> None:
        with self.assertRaises(ValueError):
            self._pool(max_workers=0)


# ── API 层单测：WS 快照/事件下发 + 日志端点 ─────────────────────────────────
class ApiWsTests(unittest.TestCase):
    """TestClient WS：连接即快照，总线事件实时转发；日志端点读采集文件。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.app = create_app(self.root, home=self.home)
        self._stack = ExitStack()
        self.client = self._stack.enter_context(TestClient(self.app))

    def tearDown(self) -> None:
        self._stack.close()
        self._tmp.cleanup()

    def _create(self) -> dict:
        response = self.client.post("/api/tasks", data={"description": "remove the ball"},
                                    files={"video": ("a.mp4", b"v", "video/mp4")})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_ws_snapshot_then_live_events(self) -> None:
        task = self._create()
        with self.client.websocket_connect("/ws/progress") as websocket:
            snapshot = websocket.receive_json()
            self.assertEqual(snapshot["type"], "snapshot")
            self.assertEqual([t["task_id"] for t in snapshot["tasks"]], [task["task_id"]])
            self.assertIn("progress", snapshot["tasks"][0])
            self.app.state.bus.publish({"type": "task_status", "task_id": task["task_id"],
                                        "status": "running"})
            event = websocket.receive_json()
            self.assertEqual(event, {"type": "task_status", "task_id": task["task_id"],
                                     "status": "running"})

    def test_log_endpoint(self) -> None:
        task = self._create()
        body = self.client.get(f"/api/tasks/{task['task_id']}/log").json()
        self.assertEqual(body["log"], "")  # 未驱动无日志
        log_dir = self.home / "logs"
        log_dir.mkdir()
        (log_dir / f"{task['task_id']}.log").write_text("[pool] 领取任务\n", encoding="utf-8")
        body = self.client.get(f"/api/tasks/{task['task_id']}/log").json()
        self.assertIn("[pool] 领取任务", body["log"])
        self.assertEqual(self.client.get("/api/tasks/missing/log").status_code, 404)


# ── E2E：fake agent 真实子进程 × API × 进程池 × WS ──────────────────────────
class FakeAgentDrive:
    """drive(task, prompt)：起 scripts/fake_agent.py 子进程按计划模拟一轮 agent。

    plans[tid] 按调用序弹出步骤列表（看门狗各轮/反馈轮连续编号）；未登记的任务用
    default_plans（上传前预置，避免与 dispatcher 领取竞态）。提示原文落盘供断言。
    """

    def __init__(self, tmp: Path, home: Path) -> None:
        self.tmp, self.home = tmp, home
        self.plans: dict[str, deque] = {}
        self.default_plans: deque = deque()
        self.prompts: dict[str, list[str]] = {}
        self.calls: dict[str, int] = {}

    def __call__(self, task: Task, prompt: str):
        index = self.calls.get(task.task_id, 0)
        self.calls[task.task_id] = index + 1
        self.prompts.setdefault(task.task_id, []).append(prompt)
        prompt_file = self.tmp / f"prompt_{task.task_id}_{index}.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        plans = self.plans.get(task.task_id) or self.default_plans
        plan_file = self.tmp / f"plan_{task.task_id}_{index}.json"
        plan_file.write_text(json.dumps(plans.popleft()), encoding="utf-8")
        command = [sys.executable, str(PROJECT_ROOT / "scripts" / "fake_agent.py"),
                   "--home", str(self.home), "--sid", task.task_id,
                   "--prompt-file", str(prompt_file), "--plan", str(plan_file)]
        completed = subprocess.run(command, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=60)
        return completed, self.home / "agents" / task.task_id, self.home / "sessions" / task.task_id


DELIVER_PLAN = [{"sleep": 0.2}, {"touch": "case/meta.json"}, {"sleep": 0.4},
                {"final": "case/final/full_480.mp4"},
                {"result": {"status": "done", "final": ""}}]  # final 由假件自动补绝对路径
STOP_PLAN = [{"touch": "case/meta.json"}, {"exit": 0}]  # 半途收工：不交 result.json


class PoolE2eTests(unittest.TestCase):
    """fake agent 子进程全链路：生命周期管理与事件推送正常（需求 5.2 通过标准）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="m52-e2e-", ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.drive = FakeAgentDrive(self.root, self.home)
        self._stack = ExitStack()

    def tearDown(self) -> None:
        self._stack.close()
        self._tmp.cleanup()

    def _app(self, **kw):
        params = dict(start_pool=True, pool_drive=self.drive, max_rounds=3, ticker_interval=0.1)
        params.update(kw)
        app = create_app(PROJECT_ROOT, home=self.home, **params)
        client = self._stack.enter_context(TestClient(app))
        return app, client

    def _upload(self, client) -> dict:
        response = client.post("/api/tasks", data={"description": "remove the ball"},
                               files={"video": ("a.mp4", b"fake-video", "video/mp4")})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _wait_status(self, client, tid: str, status: str, rounds: int | None = None) -> dict:
        def ready():
            detail = client.get(f"/api/tasks/{tid}").json()
            return detail["status"] == status and (rounds is None or detail["rounds"] == rounds)
        wait_for(ready, timeout=30, message=f"{tid} 未到 {status}")
        return client.get(f"/api/tasks/{tid}").json()

    def test_upload_auto_drive_ws_download(self) -> None:
        app, client = self._app()
        self.drive.default_plans = deque([DELIVER_PLAN])
        with client.websocket_connect("/ws/progress") as websocket:
            snapshot = websocket.receive_json()
            self.assertEqual(snapshot["type"], "snapshot")
            task = self._upload(client)
            tid = task["task_id"]
            detail = self._wait_status(client, tid, "done")  # 进程池自动领取并交付
            # WS 事件流：running → round_start/log/progress → round_end → done
            events = []
            while True:
                event = websocket.receive_json()
                events.append(event)
                if event["type"] == "task_status" and event.get("status") == "done":
                    break
            kinds = [e["type"] for e in events]
            self.assertIn("round_start", kinds)
            self.assertIn("round_end", kinds)
            self.assertIn("progress", kinds)
            self.assertIn("log", kinds)
            running = [e for e in events if e["type"] == "task_status" and e.get("status") == "running"]
            self.assertEqual(running[0]["task_id"], tid)
            done = events[-1]
            self.assertEqual(done["watchdog_rounds"], 1)
            self.assertTrue(any(e["type"] == "log" and "[fake-agent]" in e["line"] for e in events))
            self.assertTrue(any(e["type"] == "progress" and e["progress"]["ingest"] for e in events))
            # 提示口径 + 下载 + 日志采集
            self.assertIn(f"Session id: {tid}. Kind: edit.", self.drive.prompts[tid][0])
            self.assertTrue(detail["progress"]["final"])
            download = client.get(f"/api/tasks/{tid}/result")
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.content, b"fake-final-video")
            log = client.get(f"/api/tasks/{tid}/log").json()["log"]
            self.assertIn("[watchdog]", log)
            self.assertIn("[fake-agent] round done", log)

    def test_watchdog_resume_after_premature_stop(self) -> None:
        app, client = self._app()
        # 首轮半途收工（exit 0 无 result.json）→ 看门狗次轮凭恢复提示续跑交付
        # （计划上传前预置，避免与 dispatcher 领取竞态）
        self.drive.default_plans = deque([STOP_PLAN, DELIVER_PLAN])
        task = self._upload(client)
        tid = task["task_id"]
        detail = self._wait_status(client, tid, "done")
        self.assertEqual(len(self.drive.prompts[tid]), 2)
        resume = self.drive.prompts[tid][1]
        self.assertIn("This session was interrupted", resume)
        self.assertIn("status(sid) FIRST", resume)
        self.assertIn("do NOT redo completed steps", resume)
        self.assertEqual(detail["progress"]["result"], "done")
        log = client.get(f"/api/tasks/{tid}/log").json()["log"]
        self.assertIn("第 1 轮未交付", log)
        self.assertIn("第 2 轮已交付", log)

    def test_exhausted_failed_then_feedback_recovers(self) -> None:
        app, client = self._app(max_rounds=2)
        self.drive.default_plans = deque([STOP_PLAN, STOP_PLAN])
        task = self._upload(client)
        tid = task["task_id"]
        self._wait_status(client, tid, "failed")  # 耗尽裁决 failed（终态，不自动重领）
        self.assertEqual(len(self.drive.prompts[tid]), 2)
        # 反馈重制：拨回 created → 进程池自动再领取，follow-up 提示驱动交付
        self.drive.plans[tid] = deque([DELIVER_PLAN])
        response = client.post(f"/api/tasks/{tid}/feedback",
                               json={"text": "please finish the job", "windows": [0]})
        self.assertEqual(response.status_code, 200, response.text)
        detail = self._wait_status(client, tid, "done", rounds=2)
        self.assertEqual(detail["rounds"], 2)
        self.assertIn("User feedback: please finish the job", self.drive.prompts[tid][2])

    def test_restart_interrupted_resumes(self) -> None:
        # 第一次生命周期：不启池，模拟"running 中进程死亡"的现场
        app1 = create_app(PROJECT_ROOT, home=self.home)
        with TestClient(app1) as client1:
            task = self._upload(client1)
            tid = task["task_id"]
            app1.state.store.set_status(tid, "running")  # 曾领取，未交付即重启
        # 重启：recover 标 interrupted（create_app 时裁决，池尚未启动）→ 进上下文启池，
        # 凭恢复提示续跑交付
        self.drive.default_plans = deque([DELIVER_PLAN])
        app2 = create_app(PROJECT_ROOT, home=self.home, start_pool=True, pool_drive=self.drive,
                          max_rounds=3, ticker_interval=0.1)
        self.assertEqual(app2.state.store.get(tid).status, "interrupted")
        client2 = self._stack.enter_context(TestClient(app2))
        self._wait_status(client2, tid, "done")
        self.assertIn("This session was interrupted", self.drive.prompts[tid][0])
        self.assertIn("do NOT redo completed steps", self.drive.prompts[tid][0])


# ── [agent].round_timeout 接线 ──────────────────────────────────────
class PoolTimeoutWiringTests(unittest.TestCase):
    """单轮超时缺省读 config，显式注入优先，并真实送进 run_with_watchdog。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="m52-timeout-", ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        (self.home / "sessions").mkdir(parents=True)
        self.store = TaskStore(self.home / "tasks.db")
        self.bus = EventBus()
        self._pools: list[AgentPool] = []

    def tearDown(self) -> None:
        for pool in self._pools:
            pool.stop()
        self.store.close()
        self._tmp.cleanup()

    def _pool(self, **kw) -> AgentPool:
        pool = AgentPool(self.root, self.home, self.store, self.bus, log=lambda _m: None, **kw)
        self._pools.append(pool)
        return pool

    def _config(self, value: str) -> None:
        (self.root / "config.toml").write_text(f"[agent]\nround_timeout = {value}\n", encoding="utf-8")

    def test_default_timeout_reads_config(self) -> None:
        self._config("90")
        self.assertEqual(self._pool().timeout, 90.0)  # 缺省 drive 路径
        # 注入 drive 路径同样读 config（E2E/单测注入不得回退旧 600s 硬编码）
        self.assertEqual(self._pool(drive=lambda task, prompt: None).timeout, 90.0)

    def test_missing_config_defaults_7200(self) -> None:
        self.assertEqual(self._pool().timeout, 7200.0)
        self.assertEqual(self._pool(drive=lambda task, prompt: None).timeout, 7200.0)

    def test_explicit_timeout_overrides_config(self) -> None:
        self._config("90")
        self.assertEqual(self._pool(timeout=0.5).timeout, 0.5)

    def test_watchdog_receives_configured_timeout(self) -> None:
        self._config("90")
        captured: dict = {}

        def fake_watchdog(root, sid, prompt, **kw):
            captured.update(kw)
            session = self.home / "sessions" / sid
            session.mkdir(parents=True, exist_ok=True)
            (session / "result.json").write_text(
                json.dumps({"status": "done", "final": ""}), encoding="utf-8")
            return WatchdogResult(rounds=1, delivered=True, session_dir=session,
                                  prompts=[prompt], results=[subprocess.CompletedProcess([], 0)])

        pool = self._pool(drive=lambda task, prompt: None, poll_interval=0.05, ticker_interval=0.05)
        self.store.create(_task("t1"))
        with patch("app.server.pool.run_with_watchdog", fake_watchdog):
            pool.start()
            wait_for(lambda: self.store.get("t1").status == "done", message="t1 未交付")
        self.assertEqual(captured["timeout"], 90.0)


if __name__ == "__main__":
    unittest.main()