"""M5.4：阶段 9 服务入口联调——进程池缺省驱动配置接线单测 + 真 uvicorn 全链路 E2E。

零 GPU / 零 API 费（fake_llm 回放 × fake_painter × fake_comfy_worker × viggle-remake-mcp × Hermes）：
- 单测：缺省 drive（不注入 pool_drive）从 config.toml [llm] 读三元组喂 run_with_watchdog
  （M5.2 留待 5.4 的接线断言，patch 看门狗零费取证）；
- E2E：真 uvicorn（真实 WS 握手，M5.3 教训：TestClient 内存传输不走握手）+ 真 httpx /
  websockets 客户端——上传建任务 → 进程池自动领取 → WS 快照/状态/进度/日志事件 →
  详情终态与阶段进度 → 成片下载 → SPA 首页 → 反馈重制（次轮只 status→finish→save_result，
  反馈原文实进上下文）。驱动经 pool_drive 注入单轮 drive（M4.4 脚手架，真实 Hermes × MCP；
  看门狗由进程池自带，注入契约是单轮三元组而非 WatchdogResult）。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import httpx
import uvicorn
from fastapi.testclient import TestClient
from websockets.sync.client import connect as ws_connect

from app.agent.watchdog import WatchdogResult
from app.server.api import create_app
from scripts.tests.test_m43_swap_review import (E2E_READY, PROJECT_ROOT, base_entries,
                                                executed_names, result_dict, tool)
from scripts.tests.test_m44_watchdog import WatchdogHarness


# ── 缺省驱动配置接线（零费单测）──────────────────────────────────────────────
class PoolDriveWiringTests(unittest.TestCase):
    """不注入 pool_drive 时，进程池从 config.toml [llm] 取 base_url/api_key 喂看门狗。"""

    def test_default_drive_uses_config_llm(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            home = root / "home"
            (root / "config.toml").write_text(
                f"[paths]\nremake_home = \"{home.as_posix()}\"\n\n"
                "[llm]\nbase_url = \"http://127.0.0.1:9/v1\"\napi_key = \"cfg-key\"\n"
                "model = \"cfg-model\"\n\n[painter]\napi_key = \"cfg-paint\"\n", encoding="utf-8")
            captured: dict = {}

            def fake_watchdog(_root, sid, prompt, **kwargs):
                captured.update(kwargs, sid=sid, prompt=prompt)
                session = kwargs["home"] / "sessions" / sid
                (session / "result.json").write_text(
                    json.dumps({"status": "done", "final": ""}), encoding="utf-8")
                return WatchdogResult(rounds=1, delivered=True, session_dir=session,
                                      prompts=[prompt],
                                      results=[subprocess.CompletedProcess(args=["hermes"], returncode=0)])

            with mock.patch("app.server.pool.run_with_watchdog", fake_watchdog):
                app = create_app(root, start_pool=True, ticker_interval=0.2)
                with TestClient(app) as client:
                    response = client.post(
                        "/api/tasks",
                        files={"video": ("a.mp4", b"\x00\x01", "video/mp4")},
                        data={"description": "remove the ball", "mode": "edit"})
                    self.assertEqual(response.status_code, 201, response.text)
                    tid = response.json()["task_id"]
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and "sid" not in captured:
                        time.sleep(0.05)
            self.assertEqual(captured.get("sid"), tid)  # 进程池自动领取并驱动
            self.assertEqual(captured["base_url"], "http://127.0.0.1:9/v1")
            self.assertEqual(captured["api_key"], "cfg-key")
            self.assertEqual(captured["home"], home.resolve())
            self.assertEqual(captured["max_rounds"], 3)
            self.assertIn(f"Session id: {tid}. Kind: edit.", captured["prompt"])

    def test_pool_reloads_llm_key_per_task(self) -> None:
        """进程池每任务现读 config：设置页改密钥后下一任务即生效，无需重启服务。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            home = root / "home"

            def write_cfg(key: str) -> None:
                (root / "config.toml").write_text(
                    f"[paths]\nremake_home = \"{home.as_posix()}\"\n\n"
                    "[llm]\nbase_url = \"http://127.0.0.1:9/v1\"\n"
                    f"api_key = \"{key}\"\nmodel = \"cfg-model\"\n\n"
                    "[painter]\napi_key = \"cfg-paint\"\n", encoding="utf-8")

            write_cfg("k1")
            captured: list[dict] = []

            def fake_watchdog(_root, sid, prompt, **kwargs):
                captured.append(dict(kwargs, sid=sid))
                session = kwargs["home"] / "sessions" / sid
                session.mkdir(parents=True, exist_ok=True)
                (session / "result.json").write_text(
                    json.dumps({"status": "done", "final": ""}), encoding="utf-8")
                return WatchdogResult(rounds=1, delivered=True, session_dir=session,
                                      prompts=[prompt],
                                      results=[subprocess.CompletedProcess(args=["hermes"], returncode=0)])

            with mock.patch("app.server.pool.run_with_watchdog", fake_watchdog):
                app = create_app(root, start_pool=True, ticker_interval=0.2)
                with TestClient(app) as client:
                    for index in (1, 2):
                        if index == 2:
                            write_cfg("k2")  # 模拟设置页保存新密钥
                        response = client.post(
                            "/api/tasks",
                            files={"video": (f"a{index}.mp4", b"\x00\x01", "video/mp4")},
                            data={"description": f"task {index}", "mode": "edit"})
                        self.assertEqual(response.status_code, 201, response.text)
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline and len(captured) < index:
                            time.sleep(0.05)
            self.assertEqual([item["api_key"] for item in captured], ["k1", "k2"])


# ── E2E：真 uvicorn 全链路（上传→WS 进度→下载→反馈重制）─────────────────────
@unittest.skipUnless(E2E_READY, "未装 MCP venv/Hermes 运行时/ffmpeg（阶段 3/7）")
class ServiceE2eTests(unittest.TestCase):
    """start_pool=True 的服务入口全链路：真实服务器 + 真实 WS/HTTP 客户端，零费假件驱动。"""

    def _wait(self, predicate, timeout: float, message: str) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.2)
        self.fail(f"等待超时：{message}")

    @staticmethod
    def _status_event(events: list[dict], tid: str, status: str) -> bool:
        return any(e["type"] == "task_status" and e.get("task_id") == tid
                   and e.get("status") == status for e in events)

    def test_upload_ws_download_feedback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="m54-e2e-", ignore_cleanup_errors=True) as temporary:
            harness = WatchdogHarness(Path(temporary), "m54-init")
            harness.start_worker()
            harness.start_llm()

            def drive(task, prompt):
                """pool_drive 注入：单轮驱动（进程池自带看门狗），按提示选回放脚本。

                首轮全链路交付；反馈轮只 status→finish→save_result（复用原 session 现场）。
                """
                harness.sid = task.task_id
                tid = task.task_id
                deliver = [tool("finish", {"sid": tid, "picks": {"0": 0}, "canvas": "480"}),
                           tool("save_result", {"sid": tid, "result": result_dict({"0": 0}, 1)})]
                if "User feedback:" in prompt:
                    entries = [tool("status", {"sid": tid}), *deliver, {"content": "delivered again"}]
                else:
                    entries = base_entries(tid, task.src) + [*deliver, {"content": "delivered"}]
                harness.set_script(entries)
                return harness.drive(prompt)

            dist = PROJECT_ROOT / "app" / "web" / "dist"
            app = create_app(PROJECT_ROOT, home=harness.home, start_pool=True,
                             pool_drive=drive, ticker_interval=0.5, web_dist=dist)
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            events: list[dict] = []
            stack = ExitStack()
            try:
                self._wait(lambda: server.started, 15, "uvicorn 启动")
                port = server.servers[0].sockets[0].getsockname()[1]
                ws = stack.enter_context(ws_connect(f"ws://127.0.0.1:{port}/ws/progress"))  # 真实 WS 握手

                def collect():
                    try:
                        for message in ws:
                            events.append(json.loads(message))
                    except Exception:
                        pass  # 收尾关 socket

                threading.Thread(target=collect, daemon=True).start()
                self._wait(lambda: any(e["type"] == "snapshot" for e in events), 10, "WS 快照")

                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
                    # 上传建任务（swap：P1 -> Vela，预算 8，10s 区间）
                    response = client.post(
                        "/api/tasks",
                        files={"video": ("source.mp4", harness.src.read_bytes(), "video/mp4")},
                        data={"description": "replace the person with Vela", "mode": "swap",
                              "cast": json.dumps({"P1": "Vela"}), "budget": "8", "dur": "10"})
                    self.assertEqual(response.status_code, 201, response.text)
                    tid = response.json()["task_id"]
                    # 进程池自动领取 → WS 实时事件 → 交付终态
                    self._wait(lambda: self._status_event(events, tid, "done"), 300, "首轮交付")
                    types = {e["type"] for e in events}
                    self.assertTrue({"snapshot", "task_status", "progress", "log"} <= types, types)
                    self.assertTrue(self._status_event(events, tid, "running"))
                    done_event = [e for e in events if e["type"] == "task_status"
                                  and e.get("status") == "done"][-1]
                    self.assertTrue(done_event.get("final"))
                    detail = client.get(f"/api/tasks/{tid}").json()
                    self.assertEqual(detail["status"], "done")
                    self.assertEqual(detail["progress"]["result"], "done")
                    self.assertTrue(detail["progress"]["final"])
                    self.assertGreaterEqual(detail["progress"]["painted"], 2)
                    self.assertGreaterEqual(detail["progress"]["takes"], 1)
                    self.assertTrue(detail["progress"]["review"])  # 真 MCP strip → case/review/*_w*.jpg
                    self.assertTrue(client.get(f"/api/tasks/{tid}/log").json()["log"])
                    # 成片下载（字节一致）+ 中间产物静态访问
                    final = Path(detail["final"])
                    download = client.get(f"/api/tasks/{tid}/result")
                    self.assertEqual(download.status_code, 200)
                    self.assertEqual(download.content, final.read_bytes())
                    self.assertEqual(client.get(
                        f"/files/sessions/{tid}/case/paint/g2/f60.png").status_code, 200)
                    # SPA 首页（dist 已构建时；M5.3 产物）
                    if (dist / "index.html").is_file():
                        page = client.get("/")
                        self.assertEqual(page.status_code, 200)
                        self.assertIn('<div id="root">', page.text)
                    # 反馈重制：rounds+1 拨回 created → 进程池再领取 → 次轮交付
                    response = client.post(f"/api/tasks/{tid}/feedback",
                                           json={"text": "the cut at 5s feels early, re-check window 0",
                                                 "windows": [0]})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual((response.json()["status"], response.json()["rounds"]),
                                     ("created", 2))
                    self._wait(lambda: len([e for e in events if e["type"] == "task_status"
                                            and e.get("status") == "done"]) >= 2, 300, "反馈轮交付")
                    detail = client.get(f"/api/tasks/{tid}").json()
                    self.assertEqual((detail["status"], detail["rounds"]), ("done", 2))
                    # 反馈轮只 status→finish→save_result，反馈原文实进上下文
                    main = harness.requests()[-1]
                    self.assertEqual(executed_names([main]),
                                     [f"mcp__viggle_remake__{n}" for n in ("status", "finish", "save_result")])
                    self.assertIn("User feedback: the cut at 5s feels early",
                                  json.dumps(main.get("messages", []), ensure_ascii=False))
            finally:
                stack.close()
                server.should_exit = True
                thread.join(timeout=20)
                harness.close()


if __name__ == "__main__":
    unittest.main()