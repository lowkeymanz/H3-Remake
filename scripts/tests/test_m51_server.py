"""M5.1：任务 API + 上传落盘 + 角色库 API + 反馈重制 API + sqlite 持久化——零费单测 + E2E。

零 GPU / 零 API 费：
- 单测：TaskStore（CRUD/状态机/反馈续任务/重启裁决/跨开库持久化）、chars（sheet/photo/lib
  管理、名字与非图校验）、FastAPI TestClient（创建落盘/预算 paint_limit/校验/列表详情/
  进度推导/交付同步/下载/反馈重制/角色库端点/静态产物/重启恢复）；
- E2E：上传→真实驱动→下载闭环（fake_llm × fake_painter × fake_comfy_worker × MCP × Hermes，
  复用 M4.4 脚手架）——API 建任务 → 任务提示驱动全链路交付 → 详情同步终态 → 成片下载 →
  反馈重制（rounds+1 复用原 session，follow-up 提示次轮只 status→finish→save_result，
  不重做 ingest/paint/submit）。
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

from PIL import Image
from fastapi.testclient import TestClient

from app.server import chars
from app.server.api import create_app, followup_prompt, session_progress, task_prompt
from app.server.store import Task, TaskStore
from scripts.tests.test_m43_swap_review import (E2E_READY, PROJECT_ROOT, base_entries,
                                                executed_names, result_dict, tool)
from scripts.tests.test_m44_watchdog import WatchdogHarness


def _png_bytes(width: int = 64, height: int = 48) -> bytes:
    """非纯色 PNG（chars 上传校验走 Pillow 解码）。"""
    image = Image.new("RGB", (width, height))
    image.putdata([(x * 4 % 256, y * 4 % 256, (x + y) % 256)
                   for y in range(height) for x in range(width)])
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


# ── TaskStore 单测 ───────────────────────────────────────────────────────────
class TaskStoreTests(unittest.TestCase):
    """sqlite 任务表：CRUD、状态机、反馈续任务、重启裁决、跨开库持久化。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.store = TaskStore(self.root / "tasks.db")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _task(self, task_id: str = "tabc123", **kw) -> Task:
        defaults = dict(mode="swap", description="replace the person", src="D:/clips/a.mp4",
                        cast={"P1": "Vela"}, budget=8, start_s=1.0, dur_s=5.0)
        defaults.update(kw)
        return self.store.create(Task(task_id=task_id, **defaults))

    def test_create_get_roundtrip(self) -> None:
        self._task()
        got = self.store.get("tabc123")
        self.assertIsNotNone(got)
        self.assertEqual(got.cast, {"P1": "Vela"})  # JSON 列反序列化
        self.assertEqual(got.status, "created")
        self.assertEqual(got.rounds, 1)
        self.assertEqual(got.feedback, [])
        self.assertEqual((got.budget, got.start_s, got.dur_s), (8, 1.0, 5.0))
        self.assertGreater(got.created_at, 0)
        self.assertIsNone(self.store.get("missing"))

    def test_list_newest_first(self) -> None:
        self._task("tfirst")
        self._task("tsecond")
        self.assertEqual([t.task_id for t in self.store.list()], ["tsecond", "tfirst"])

    def test_set_status_and_final(self) -> None:
        self._task()
        task = self.store.set_status("tabc123", "done", final="D:/out/full_480.mp4")
        self.assertEqual((task.status, task.final), ("done", "D:/out/full_480.mp4"))
        with self.assertRaises(AssertionError):
            self.store.set_status("tabc123", "bogus")

    def test_add_feedback_reopens_terminal_task(self) -> None:
        self._task()
        self.store.set_status("tabc123", "done")
        task = self.store.add_feedback("tabc123", {"text": "cut feels early", "windows": [0]})
        self.assertEqual(task.status, "created")  # 拨回待驱动（复用原 session）
        self.assertEqual(task.rounds, 2)
        self.assertEqual(task.feedback, [{"text": "cut feels early", "windows": [0]}])
        self.store.set_status("tabc123", "done")  # 再次交付后才可再反馈
        task = self.store.add_feedback("tabc123", {"text": "again"})
        self.assertEqual(task.rounds, 3)
        self.assertEqual(len(task.feedback), 2)

    def test_add_feedback_rejects_non_terminal(self) -> None:
        self._task()
        for status in ("created", "running", "interrupted"):
            self.store.set_status("tabc123", status)
            with self.assertRaises(ValueError):
                self.store.add_feedback("tabc123", {"text": "x"})

    def test_recover_adjudicates_running_tasks(self) -> None:
        self._task("tdone")
        self._task("tlost")
        self._task("tidle")  # created 不受重启裁决影响
        self.store.set_status("tdone", "running")
        self.store.set_status("tlost", "running")
        session = self.root / "sessions" / "tdone"
        (session / "case" / "final").mkdir(parents=True)
        final = session / "case" / "final" / "full_480.mp4"
        final.write_bytes(b"video")
        (session / "result.json").write_text(json.dumps({"status": "done", "final": str(final)}),
                                             encoding="utf-8")
        changed = self.store.recover(lambda sid: self.root / "sessions" / sid)
        self.assertEqual({t.task_id for t in changed}, {"tdone", "tlost"})
        self.assertEqual(self.store.get("tdone").status, "done")
        self.assertEqual(self.store.get("tdone").final, str(final))
        self.assertEqual(self.store.get("tlost").status, "interrupted")  # 待看门狗续跑（M4.4）
        self.assertEqual(self.store.get("tidle").status, "created")

    def test_persistence_across_reopen(self) -> None:
        self._task()
        self.store.set_status("tabc123", "running")
        self.store.close()
        self.store = TaskStore(self.root / "tasks.db")  # 重启：同库重开
        got = self.store.get("tabc123")
        self.assertEqual((got.status, got.cast, got.description),
                         ("running", {"P1": "Vela"}, "replace the person"))


# ── chars 角色库单测 ─────────────────────────────────────────────────────────
class CharsTests(unittest.TestCase):
    """chars/ 管理：sheet/photo/lib_chars.json，与 MCP sheet(source="lib") 口径对齐。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self._tmp.name) / "chars"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_add_sheet_and_list(self) -> None:
        entry = chars.add_sheet(self.dir, "Vela", _png_bytes(), desc="a woman", outfit="teal jacket")
        self.assertEqual(entry["name"], "Vela")
        self.assertIsNone(entry["photo"])
        self.assertTrue((self.dir / "Vela_sheet.png").is_file())
        lib = json.loads((self.dir / "lib_chars.json").read_text(encoding="utf-8"))
        self.assertEqual(lib, {"chars": {"Vela": "a woman"}, "outfits": {"Vela": "teal jacket"}})
        self.assertEqual(chars.list_chars(self.dir), [entry])

    def test_sheet_overwrite_keeps_single_entry(self) -> None:
        chars.add_sheet(self.dir, "Vela", _png_bytes())
        chars.add_sheet(self.dir, "Vela", _png_bytes(32, 32))
        self.assertEqual(len(chars.list_chars(self.dir)), 1)

    def test_photo_requires_sheet_then_lands(self) -> None:
        with self.assertRaises(chars.CharsError):
            chars.add_photo(self.dir, "Vela", _png_bytes())
        chars.add_sheet(self.dir, "Vela", _png_bytes())
        entry = chars.add_photo(self.dir, "Vela", _png_bytes())
        self.assertEqual(entry["photo"], "Vela_photo.png")
        self.assertTrue((self.dir / "Vela_photo.png").is_file())

    def test_remove_cleans_files_and_lib(self) -> None:
        chars.add_sheet(self.dir, "Vela", _png_bytes(), desc="d", outfit="o")
        chars.add_photo(self.dir, "Vela", _png_bytes())
        chars.remove_char(self.dir, "Vela")
        self.assertEqual(chars.list_chars(self.dir), [])
        self.assertFalse((self.dir / "Vela_photo.png").exists())
        lib = json.loads((self.dir / "lib_chars.json").read_text(encoding="utf-8"))
        self.assertEqual(lib, {"chars": {}, "outfits": {}})
        with self.assertRaises(chars.CharsError):
            chars.remove_char(self.dir, "Vela")

    def test_remove_keeps_other_lib_files(self) -> None:
        chars.add_sheet(self.dir, "Vela", _png_bytes(), desc="d")
        other = self.dir / "lib_other.json"
        other.write_text(json.dumps({"chars": {"X": "y"}}), encoding="utf-8")
        chars.remove_char(self.dir, "Vela")
        self.assertEqual(json.loads(other.read_text(encoding="utf-8")), {"chars": {"X": "y"}})

    def test_invalid_names_rejected(self) -> None:
        for name in ("", "bad.name", "../x", "a/b"):
            with self.assertRaises(chars.CharsError, msg=name):
                chars.add_sheet(self.dir, name, _png_bytes())

    def test_non_image_rejected(self) -> None:
        with self.assertRaises(chars.CharsError):
            chars.add_sheet(self.dir, "Vela", b"not an image")

    def test_list_missing_dir_empty(self) -> None:
        self.assertEqual(chars.list_chars(self.dir), [])


# ── FastAPI TestClient 单测 ──────────────────────────────────────────────────
class ApiTests(unittest.TestCase):
    """任务/角色库/反馈 API：CRUD、落盘路径、预算、进度、交付同步、下载、重启恢复。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        # 创建任务前置校验要求密钥齐备：夹具预置双密钥（镜像口径由用户在设置页填写）
        (self.root / "config.toml").write_text(
            '[llm]\napi_key = "test-llm"\n\n[painter]\napi_key = "test-painter"\n', encoding="utf-8")
        self.app = create_app(self.root, home=self.home)
        self.store = self.app.state.store
        self._stack = ExitStack()
        self.client = self._stack.enter_context(TestClient(self.app))

    def tearDown(self) -> None:
        self._stack.close()
        self._tmp.cleanup()

    def _create(self, **overrides) -> dict:
        data = {"description": "remove the ball", "mode": "edit"}
        data.update(overrides)
        files = {"video": ("clip.mp4", b"fake-video-bytes", "video/mp4")}
        response = self.client.post("/api/tasks", data=data, files=files)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _deliver(self, task_id: str, status: str = "done") -> Path:
        """模拟 agent 交付：写成片 + result.json，任务拨 running（详情/下载时同步裁决）。"""
        session = self.home / "sessions" / task_id
        final = session / "case" / "final" / "full_480.mp4"
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"fake-final-video")
        (session / "result.json").write_text(json.dumps({"status": status, "final": str(final)}),
                                             encoding="utf-8")
        self.store.set_status(task_id, "running")
        return final

    def test_create_requires_api_keys(self):
        """开箱无密钥：创建被 400 拦截，文案引导去设置页填写。"""
        (self.root / "config.toml").write_text('[llm]\napi_key = ""\n', encoding="utf-8")
        response = self.client.post(
            "/api/tasks", data={"description": "x", "mode": "edit"},
            files={"video": ("clip.mp4", b"v", "video/mp4")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("设置", response.json()["detail"])

    def test_create_edit_task_lands_upload(self) -> None:
        task = self._create()
        self.assertRegex(task["task_id"], r"^t[0-9a-f]{12}$")  # MCP sid 口径
        self.assertEqual(task["status"], "created")
        self.assertEqual(task["mode"], "edit")
        src = Path(task["src"])
        self.assertEqual(src, self.home / "uploads" / task["task_id"] / "source.mp4")
        self.assertEqual(src.read_bytes(), b"fake-video-bytes")
        session = self.home / "sessions" / task["task_id"]
        self.assertEqual((session / "kind").read_text(), "edit")  # new_session 幂等落位
        self.assertFalse((session / "paint_limit").exists())  # 未给预算不写覆盖文件

    def test_create_swap_writes_budget_and_cast(self) -> None:
        task = self._create(mode="swap", description="replace the person",
                            cast=json.dumps({"P1": "Vela"}), budget="8", start="1", dur="5")
        self.assertEqual(task["cast"], {"P1": "Vela"})
        self.assertEqual((task["budget"], task["start"], task["dur"]), (8, 1.0, 5.0))
        session = self.home / "sessions" / task["task_id"]
        self.assertEqual((session / "kind").read_text(), "swap")
        self.assertEqual((session / "paint_limit").read_text(), "8")  # per-session 预算覆盖

    def test_create_validation(self) -> None:
        cases = [
            {"mode": "bogus"}, {"description": "  "},
            {"mode": "swap"},  # swap 无 cast
            {"mode": "swap", "cast": "{not json"},
            {"mode": "swap", "cast": json.dumps(["P1"])},
            {"budget": "0"}, {"dur": "-1"}, {"start": "-0.5"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                data = {"description": "x", "mode": "edit", **overrides}
                response = self.client.post("/api/tasks", data=data,
                                            files={"video": ("a.mp4", b"v", "video/mp4")})
                self.assertEqual(response.status_code, 400, response.text)
        for name, content in (("a.txt", b"v"), ("a.mp4", b"")):
            response = self.client.post("/api/tasks", data={"description": "x"},
                                        files={"video": (name, content, "video/mp4")})
            self.assertEqual(response.status_code, 400, response.text)

    def test_list_detail_and_404(self) -> None:
        first, second = self._create(), self._create()
        listed = self.client.get("/api/tasks").json()
        self.assertEqual({t["task_id"] for t in listed}, {first["task_id"], second["task_id"]})
        detail = self.client.get(f"/api/tasks/{first['task_id']}").json()
        self.assertEqual(detail["task_id"], first["task_id"])
        self.assertIn("progress", detail)
        self.assertEqual(self.client.get("/api/tasks/missing").status_code, 404)

    def test_progress_from_session_artifacts(self) -> None:
        task = self._create()
        case = self.home / "sessions" / task["task_id"] / "case"
        for sub in ("paint/g2", "paint/src", "takes", "look", "review", "final"):
            (case / sub).mkdir(parents=True)
        (case / "meta.json").write_text("{}", encoding="utf-8")
        (case / "plan_grid.json").write_text("{}", encoding="utf-8")
        (case / "paint" / "g2" / "f60.png").write_bytes(_png_bytes())
        (case / "paint" / "src" / "f60.png").write_bytes(_png_bytes())  # src 帧不算绘制
        (case / "takes" / "w0_480_s0_1.mp4").write_bytes(b"t")
        (case / "look" / "strips_0.jpg").write_bytes(b"s")  # ingest 镜头条带，不是审查产物
        (case / "review" / "480_w0.jpg").write_bytes(b"r")
        (case / "final" / "full_480.mp4").write_bytes(b"f")
        progress = self.client.get(f"/api/tasks/{task['task_id']}").json()["progress"]
        self.assertEqual(progress, {"ingest": True, "plan": True, "painted": 1, "takes": 1,
                                    "review": True, "final": True, "result": None})

    def test_review_progress_requires_take_and_review_strip(self) -> None:
        """回归：ingest 的镜头条带不能点亮审查，审查须晚于渲染。"""
        task = self._create()
        case = self.home / "sessions" / task["task_id"] / "case"
        for sub in ("look", "review", "takes"):
            (case / sub).mkdir(parents=True)

        def review() -> bool:
            return self.client.get(f"/api/tasks/{task['task_id']}").json()["progress"]["review"]

        (case / "look" / "strips_0.jpg").write_bytes(b"s")  # 读片完成即存在
        self.assertFalse(review())
        (case / "review" / "480_w0.jpg").write_bytes(b"r")  # 无 take 的审查图也不算
        self.assertFalse(review())
        (case / "takes" / "w0_480_s0_1.mp4").write_bytes(b"t")
        self.assertTrue(review())

    def test_detail_syncs_running_delivery(self) -> None:
        task = self._create()
        final = self._deliver(task["task_id"])
        detail = self.client.get(f"/api/tasks/{task['task_id']}").json()
        self.assertEqual(detail["status"], "done")
        self.assertEqual(detail["final"], str(final))
        self.assertEqual(detail["progress"]["result"], "done")
        self.assertEqual(self.store.get(task["task_id"]).status, "done")  # 裁决落库

    def test_download_result(self) -> None:
        task = self._create()
        self.assertEqual(self.client.get(f"/api/tasks/{task['task_id']}/result").status_code, 409)
        final = self._deliver(task["task_id"])
        response = self.client.get(f"/api/tasks/{task['task_id']}/result")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, final.read_bytes())
        self.assertEqual(self.client.get("/api/tasks/missing/result").status_code, 404)

    def test_feedback_reopens_and_builds_followup(self) -> None:
        task = self._create()
        self.assertEqual(self.client.post(f"/api/tasks/{task['task_id']}/feedback",
                                          json={"text": "x"}).status_code, 409)  # 非终态
        self._deliver(task["task_id"])
        self.client.get(f"/api/tasks/{task['task_id']}")  # 同步终态
        response = self.client.post(f"/api/tasks/{task['task_id']}/feedback",
                                    json={"text": "the cut at 5s feels early", "windows": [0]})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["status"], body["rounds"]), ("created", 2))
        self.assertEqual(len(body["feedback"]), 1)
        prompt = body["prompt"]
        self.assertIn("User feedback: the cut at 5s feels early", prompt)
        self.assertIn("Windows the feedback names: w0", prompt)
        self.assertIn("status(sid) FIRST", prompt)
        self.assertIn("do NOT redo completed steps", prompt)
        self.assertIn(f"Session id: {task['task_id']}.", prompt)
        self._deliver(task["task_id"])  # 反馈轮重新交付后才可再反馈
        self.client.get(f"/api/tasks/{task['task_id']}")
        again = self.client.post(f"/api/tasks/{task['task_id']}/feedback", json={"text": "more"})
        self.assertEqual(again.json()["rounds"], 3)
        for bad in ({"text": " "}, {"text": "x", "windows": [-1]}, {"text": "x", "windows": "0"},
                    {"text": "x", "windows": [True]}):
            self.assertEqual(self.client.post(f"/api/tasks/{task['task_id']}/feedback",
                                              json=bad).status_code, 400)

    def test_feedback_rejects_window_beyond_refs(self) -> None:
        """点名窗口须落在真实窗口数内（refs.json 给出窗口数）。"""
        task = self._create()
        refs = self.home / "sessions" / task["task_id"] / "case" / "refs.json"
        refs.parent.mkdir(parents=True, exist_ok=True)
        refs.write_text(json.dumps({"windows": [{"own": [0, 90]}, {"own": [90, 180]}]}),
                        encoding="utf-8")
        self._deliver(task["task_id"])
        self.client.get(f"/api/tasks/{task['task_id']}")  # 同步终态
        url = f"/api/tasks/{task['task_id']}/feedback"
        bad = self.client.post(url, json={"text": "fix", "windows": [2]})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("0..1", bad.json()["detail"])
        ok = self.client.post(url, json={"text": "fix", "windows": [1]})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertIn("Windows the feedback names: w1", ok.json()["prompt"])

    def test_chars_api_and_static_preview(self) -> None:
        response = self.client.post("/api/chars/Vela/sheet",
                                    files={"file": ("sheet.png", _png_bytes(), "image/png")},
                                    data={"desc": "a woman", "outfit": "teal jacket"})
        self.assertEqual(response.status_code, 200, response.text)
        listed = self.client.get("/api/chars").json()
        self.assertEqual(listed[0]["name"], "Vela")
        self.assertEqual(listed[0]["desc"], "a woman")
        self.assertEqual(self.client.get("/files/chars/Vela_sheet.png").status_code, 200)
        response = self.client.post("/api/chars/Vela/photo",
                                    files={"file": ("p.png", _png_bytes(), "image/png")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/chars").json()[0]["photo"], "Vela_photo.png")
        self.assertEqual(self.client.delete("/api/chars/Vela").status_code, 200)
        self.assertEqual(self.client.get("/api/chars").json(), [])
        self.assertEqual(self.client.delete("/api/chars/Vela").status_code, 404)
        self.assertEqual(self.client.post("/api/chars/bad.name/sheet",
                                          files={"file": ("s.png", _png_bytes(), "image/png")}).status_code, 400)
        self.assertEqual(self.client.post("/api/chars/Vela/sheet",
                                          files={"file": ("s.png", b"not image", "image/png")}).status_code, 400)
        self.assertEqual(self.client.post("/api/chars/Ghost/photo",
                                          files={"file": ("p.png", _png_bytes(), "image/png")}).status_code, 400)

    def test_restart_recovery(self) -> None:
        delivered, lost = self._create(), self._create()
        self._deliver(delivered["task_id"])
        self.store.set_status(lost["task_id"], "running")  # 重启时未交付
        self._stack.close()  # 停旧服务（关库）
        app2 = create_app(self.root, home=self.home)  # 重启：同 home 重建
        with TestClient(app2) as client2:
            tasks = {t["task_id"]: t for t in client2.get("/api/tasks").json()}
            self.assertEqual(tasks[delivered["task_id"]]["status"], "done")
            self.assertTrue(tasks[delivered["task_id"]]["final"])
            self.assertEqual(tasks[lost["task_id"]]["status"], "interrupted")


# ── 提示组装单测（任务表 → 驱动提示口径）────────────────────────────────────
class PromptTests(unittest.TestCase):
    """task_prompt / followup_prompt：与 M3.3 任务口径、AGENTS.md follow-up 机制对齐。"""

    def _task(self, **kw) -> Task:
        defaults = dict(task_id="tabc", mode="swap", description="replace the person",
                        src="D:/clips/a.mp4", cast={"P1": "Vela"}, budget=8, dur_s=10.0)
        defaults.update(kw)
        return Task(**defaults)

    def test_task_prompt_swap_and_edit(self) -> None:
        prompt = task_prompt(self._task())
        self.assertIn("Session id: tabc. Kind: swap.", prompt)
        self.assertIn("Cast: P1 -> Vela", prompt)
        self.assertIn("Image-call budget for this session: 8.", prompt)
        edit = task_prompt(self._task(mode="edit", cast={}, description="remove the ball"))
        self.assertIn("Kind: edit.", edit)
        self.assertIn("Edit: remove the ball", edit)
        self.assertNotIn("Cast:", edit)

    def test_followup_prompt_reuses_session(self) -> None:
        task = self._task(rounds=2, feedback=[{"text": "cut feels early", "windows": [0]}])
        prompt = followup_prompt(task)
        self.assertIn("Session id: tabc.", prompt)  # 复用原 session
        self.assertIn("round 2", prompt)
        self.assertIn("User feedback: cut feels early", prompt)
        self.assertIn("Windows the feedback names: w0", prompt)
        self.assertIn("status(sid) FIRST", prompt)
        self.assertIn("keep the delivered takes of every other window", prompt)
        self.assertIn("finish and save_result", prompt)

    def test_session_progress_empty(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            self.assertEqual(session_progress(Path(temporary)),
                             {"ingest": False, "plan": False, "painted": 0, "takes": 0,
                              "review": False, "final": False, "result": None})


# ── E2E：上传→真实驱动→下载→反馈重制（零 GPU / 零 API 费）───────────────────
@unittest.skipUnless(E2E_READY, "未装 MCP venv/Hermes 运行时/ffmpeg（阶段 3/7）")
class ServerE2eTests(unittest.TestCase):
    """API 建任务 → 任务提示驱动全链路交付 → 详情/下载 → 反馈重制次轮不重做已完成步骤。"""

    def test_upload_drive_download_feedback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="m51-e2e-", ignore_cleanup_errors=True) as temporary:
            harness = WatchdogHarness(Path(temporary), "m51-init")
            harness.start_worker()
            harness.start_llm()
            app = create_app(PROJECT_ROOT, home=harness.home)
            try:
                with TestClient(app) as client:
                    self.assertEqual(client.get("/api/chars").json()[0]["name"], "Vela")
                    response = client.post(
                        "/api/tasks",
                        files={"video": ("source.mp4", harness.src.read_bytes(), "video/mp4")},
                        data={"description": "replace the person with Vela", "mode": "swap",
                              "cast": json.dumps({"P1": "Vela"}), "budget": "8", "dur": "10"})
                    self.assertEqual(response.status_code, 201, response.text)
                    task = response.json()
                    tid = task["task_id"]
                    harness.sid = tid  # 脚手架按 task_id 落工作目录/会话
                    store = app.state.store
                    session = harness.home / "sessions" / tid
                    self.assertEqual((session / "kind").read_text(), "swap")
                    self.assertEqual((session / "paint_limit").read_text(), "8")
                    self.assertEqual(Path(task["src"]).read_bytes(), harness.src.read_bytes())
                    store.set_status(tid, "running")  # 模拟 5.2 进程池领取驱动
                    prompt = task_prompt(store.get(tid))
                    self.assertIn(f"Session id: {tid}. Kind: swap.", prompt)
                    round1 = base_entries(tid, task["src"]) + [
                        tool("finish", {"sid": tid, "picks": {"0": 0}, "canvas": "480"}),
                        tool("save_result", {"sid": tid, "result": result_dict({"0": 0}, 1)}),
                        {"content": "delivered"}]
                    harness.set_script(round1)
                    completed, _, _ = harness.drive(prompt)
                    self.assertEqual(completed.returncode, 0,
                                     (completed.stderr or "")[-2000:])
                    # 详情同步交付终态 + 阶段进度
                    detail = client.get(f"/api/tasks/{tid}").json()
                    self.assertEqual(detail["status"], "done")
                    self.assertEqual(detail["progress"]["result"], "done")
                    self.assertTrue(detail["progress"]["final"])
                    self.assertGreaterEqual(detail["progress"]["painted"], 2)
                    self.assertGreaterEqual(detail["progress"]["takes"], 1)
                    # 成片下载 + 中间产物静态访问
                    final = Path(detail["final"])
                    self.assertTrue(final.is_file())
                    download = client.get(f"/api/tasks/{tid}/result")
                    self.assertEqual(download.status_code, 200)
                    self.assertEqual(download.content, final.read_bytes())
                    self.assertEqual(client.get(
                        f"/files/sessions/{tid}/case/paint/g2/f60.png").status_code, 200)
                    # 反馈重制：rounds+1 拨回 created，follow-up 提示复用原 session
                    response = client.post(f"/api/tasks/{tid}/feedback",
                                           json={"text": "the cut at 5s feels early, re-check window 0",
                                                "windows": [0]})
                    self.assertEqual(response.status_code, 200, response.text)
                    feedback = response.json()
                    self.assertEqual((feedback["status"], feedback["rounds"]), ("created", 2))
                    followup = feedback["prompt"]
                    store.set_status(tid, "running")
                    harness.set_script([
                        tool("status", {"sid": tid}),
                        tool("finish", {"sid": tid, "picks": {"0": 0}, "canvas": "480"}),
                        tool("save_result", {"sid": tid, "result": result_dict({"0": 0}, 1)}),
                        {"content": "delivered again"}])
                    completed, _, _ = harness.drive(followup)
                    self.assertEqual(completed.returncode, 0,
                                     (completed.stderr or "")[-2000:])
                    detail = client.get(f"/api/tasks/{tid}").json()
                    self.assertEqual((detail["status"], detail["rounds"]), ("done", 2))
                    # 反馈轮只 status→finish→save_result：不重做 ingest/paint/submit，
                    # 反馈原文实进上下文（末请求即反馈轮完整历史，oneshot 新会话）
                    main = harness.requests()[-1]
                    self.assertEqual(executed_names([main]),
                                     [f"mcp__viggle_remake__{n}" for n in ("status", "finish", "save_result")])
                    self.assertIn("User feedback: the cut at 5s feels early",
                                  json.dumps(main.get("messages", []), ensure_ascii=False))
            finally:
                harness.close()


if __name__ == "__main__":
    unittest.main()