"""M4.4：看门狗 + 会话恢复——交付判定/恢复提示单测 + 看门狗循环单测 + kill/重启续跑 E2E。

零 GPU / 零 API 费回归（fake_llm 回放 × fake_painter × fake_comfy_worker × viggle-remake-mcp × Hermes）：
- 单测：result_status / session_delivered（result.json 终态判定）、resume_task_prompt（先 status、
  不重做已完成步骤）、run_with_watchdog 循环（首轮交付 / 半途收工续跑 / kill 续跑 / 超时续跑 /
  有界耗尽 / max_rounds 校验）——drive 注入假驱动模拟各路径，零费；
- E2E：真实 Hermes × MCP × fake_llm 验证"agent 进程半途终止后重启续跑且不重做已完成步骤"——
  第 1 轮建到 take 落位后终止（半途收工 / 真 kill 进程），看门狗检测未交付 → 第 2 轮凭恢复提示
  续跑（status → finish → save_result），断言续跑轮不重做 ingest/paint/submit、result.json 交付。

恢复成立靠 MCP 持久化（new_session 幂等 / status 恢复现场 / case 与队列跨进程存活），见 app/agent/watchdog.py。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from app.agent.hermes import (set_discovery_timeout, set_model_fields,
                               set_tool_search_enabled, upsert_mcp_server)
from app.agent.run import edit_task_prompt, hermes_chat_command, run_edit_session
from app.agent.watchdog import (WatchdogResult, result_status, resume_task_prompt,
                                 run_with_watchdog, session_delivered)
from app.agent.workspace import prepare_workspace
from scripts.fake_llm import Handler, Replay
from scripts.fake_painter import Handler as PainterHandler
from scripts.tests.test_m43_swap_review import (CASE, E2E_READY, FFMPEG, HERMES_TEMPLATE,
                                                MCP_EXE, PLAN, PROJECT_ROOT, SWAP_PROMPT,
                                                WHERE, _make_source, _rich_png, base_entries,
                                                executed_names, result_dict, tool, tool_args,
                                                tool_results)

SID = "m44-watchdog"


# ── 交付判定单测 ─────────────────────────────────────────────────────────────
class ResultStatusTests(unittest.TestCase):
    """result_status / session_delivered：result.json 终态（done/partial/failed）才算交付。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.session = Path(self._tmp.name) / "sessions" / SID
        self.session.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, obj) -> None:
        (self.session / "result.json").write_text(json.dumps(obj), encoding="utf-8")

    def test_missing_result_json_not_delivered(self) -> None:
        self.assertIsNone(result_status(self.session))
        self.assertFalse(session_delivered(self.session))

    def test_done_is_delivered(self) -> None:
        self._write({"status": "done", "final": "full_480.mp4"})
        self.assertEqual(result_status(self.session), "done")
        self.assertTrue(session_delivered(self.session))

    def test_partial_and_failed_are_terminal(self) -> None:
        for status in ("partial", "failed"):
            self._write({"status": status})
            self.assertTrue(session_delivered(self.session), status)  # 终态裁决：看门狗不再续跑

    def test_non_terminal_status_not_delivered(self) -> None:
        self._write({"status": "running"})
        self.assertFalse(session_delivered(self.session))

    def test_invalid_json_not_delivered(self) -> None:
        (self.session / "result.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(result_status(self.session))
        self.assertFalse(session_delivered(self.session))

    def test_no_status_field_not_delivered(self) -> None:
        self._write({"final": "full_480.mp4"})
        self.assertIsNone(result_status(self.session))
        self.assertFalse(session_delivered(self.session))


# ── 恢复提示单测 ─────────────────────────────────────────────────────────────
class ResumePromptTests(unittest.TestCase):
    """resume_task_prompt：同口径任务字段 + 先 status、不重做已完成步骤的恢复指令。"""

    def test_resume_prompt_fields(self) -> None:
        prompt = resume_task_prompt(SID, "D:/clips/a.mp4", kind="swap", start=1.0, dur=5.0,
                                    cast={"P1": "Vela"}, budget=8)
        self.assertIn(f"Session id: {SID}. Kind: swap.", prompt)
        self.assertIn("Source clip: D:/clips/a.mp4. Span: [1, 6) (seconds).", prompt)
        self.assertIn("Cast: P1 -> Vela", prompt)
        self.assertIn("Image-call budget for this session: 8.", prompt)

    def test_resume_prompt_status_first_and_no_redo(self) -> None:
        prompt = resume_task_prompt(SID, "a.mp4", kind="swap", dur=8.0, cast={"P1": "Vela"})
        self.assertIn("status(sid) FIRST", prompt)
        self.assertIn("do NOT redo completed steps", prompt)
        self.assertIn("no re-ingest", prompt)
        self.assertIn("finish and save_result", prompt)

    def test_resume_prompt_edit_sentence(self) -> None:
        prompt = resume_task_prompt(SID, "a.mp4", edit="remove the ball")
        self.assertIn("Edit: remove the ball", prompt)
        self.assertIn("Kind: edit.", prompt)


# ── 看门狗循环单测（注入假 drive，零费）──────────────────────────────────────
class WatchdogLoopTests(unittest.TestCase):
    """run_with_watchdog：首轮交付 / 半途收工 / kill / 超时续跑 / 有界耗尽 / 参数校验。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self._tmp.name) / "home"
        self.session = self.home / "sessions" / SID
        self.session.mkdir(parents=True)
        self.logs: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _drive(self, deliver_on: int, *, kill_first: bool = False, timeout_first: bool = False):
        """假 drive：第 deliver_on 轮写 result.json 交付；可模拟首轮 kill / 超时。返回 (drive, 计数)。"""
        state = {"n": 0}

        def drive(prompt: str):
            state["n"] += 1
            n = state["n"]
            if timeout_first and n == 1:
                raise subprocess.TimeoutExpired(cmd="hermes", timeout=600)
            if n == deliver_on:
                (self.session / "result.json").write_text(
                    json.dumps({"status": "done", "final": "full_480.mp4"}), encoding="utf-8")
            code = -15 if (kill_first and n == 1) else 0
            return subprocess.CompletedProcess(args=["hermes"], returncode=code), self.home, self.session

        return drive, state

    def _run(self, drive, *, max_rounds: int = 3) -> WatchdogResult:
        return run_with_watchdog(
            PROJECT_ROOT, SID, "initial prompt", base_url="http://127.0.0.1:9/v1",
            api_key="fake-key", resume_prompt="resume prompt", home=self.home,
            max_rounds=max_rounds, drive=drive, log=self.logs.append)

    def test_delivered_first_round_no_resume(self) -> None:
        drive, state = self._drive(deliver_on=1)
        result = self._run(drive)
        self.assertTrue(result.delivered)
        self.assertEqual(result.rounds, 1)
        self.assertEqual(state["n"], 1)
        self.assertEqual(result.prompts, ["initial prompt"])  # 未触发恢复提示

    def test_resume_after_premature_stop(self) -> None:
        drive, state = self._drive(deliver_on=2)  # 首轮 returncode 0 但未交付（半途收工）
        result = self._run(drive)
        self.assertTrue(result.delivered)
        self.assertEqual(result.rounds, 2)
        self.assertEqual(state["n"], 2)
        self.assertEqual(result.prompts, ["initial prompt", "resume prompt"])
        self.assertEqual(result.results[0].returncode, 0)

    def test_resume_after_kill(self) -> None:
        drive, state = self._drive(deliver_on=2, kill_first=True)  # 首轮被 kill（returncode -15）
        result = self._run(drive)
        self.assertTrue(result.delivered)
        self.assertEqual(result.rounds, 2)
        self.assertEqual(result.results[0].returncode, -15)
        self.assertEqual(result.prompts[1], "resume prompt")

    def test_resume_after_timeout(self) -> None:
        drive, state = self._drive(deliver_on=2, timeout_first=True)  # 首轮超时被终止
        result = self._run(drive)
        self.assertTrue(result.delivered)
        self.assertEqual(result.rounds, 2)
        self.assertIsNone(result.results[0])  # 超时轮无进程结果
        self.assertEqual(state["n"], 2)

    def test_exhausts_max_rounds_not_delivered(self) -> None:
        drive, state = self._drive(deliver_on=99)  # 永不交付
        result = self._run(drive, max_rounds=3)
        self.assertFalse(result.delivered)
        self.assertEqual(result.rounds, 3)
        self.assertEqual(state["n"], 3)
        self.assertEqual(result.prompts, ["initial prompt", "resume prompt", "resume prompt"])

    def test_max_rounds_validation(self) -> None:
        drive, _ = self._drive(deliver_on=1)
        with self.assertRaises(ValueError):
            self._run(drive, max_rounds=0)

    def test_partial_delivery_stops_watchdog(self) -> None:
        state = {"n": 0}

        def drive(prompt: str):
            state["n"] += 1
            (self.session / "result.json").write_text(json.dumps({"status": "partial"}), encoding="utf-8")
            return subprocess.CompletedProcess(args=["hermes"], returncode=0), self.home, self.session

        result = self._run(drive)
        self.assertTrue(result.delivered)  # partial 是终态裁决
        self.assertEqual(result.rounds, 1)
        self.assertEqual(state["n"], 1)


# ── E2E 脚手架（持久 fake_llm + 按轮重置脚本 + 可 kill 首轮）──────────────────
class WatchdogHarness:
    """M4.4 E2E 脚手架：home / 两镜头源 / 角色库 / 假 painter / 假 worker / 持久假 LLM / Hermes 配置。"""

    def __init__(self, tmp: Path, sid: str) -> None:
        self.tmp, self.sid = tmp, sid
        self.home = tmp / "home"
        (self.home / "sessions").mkdir(parents=True)
        self.src = tmp / "src.mp4"
        _make_source(self.src)
        chars = self.home / "chars"
        _rich_png(chars / "Vela_sheet.png", 512, 768)
        (chars / "lib_chars.json").write_text(json.dumps(CASE, ensure_ascii=False), encoding="utf-8")
        self.painter_log = tmp / "painter.jsonl"
        PainterHandler.log_path = self.painter_log
        PainterHandler.colour = (96, 130, 80)
        self.painter = ThreadingHTTPServer(("127.0.0.1", 0), PainterHandler)
        threading.Thread(target=self.painter.serve_forever, daemon=True).start()
        self.hermes_home = self.home / "hermes"
        self.hermes_home.mkdir()
        self.path_env = f"{Path(FFMPEG).parent};{os.environ['PATH']}"
        self.worker = None
        self.llm = None
        self.llm_log = tmp / "llm.jsonl"
        self.llm_url = None

    def start_worker(self) -> None:
        command = [sys.executable, str(PROJECT_ROOT / "scripts/fake_comfy_worker.py"),
                   "--home", str(self.home), "--beat-every", "2", "--poll", "1",
                   "--max-480", "345", "--max-736", "175"]
        self.worker = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                                       env={**os.environ, "PATH": self.path_env, "PYTHONUTF8": "1"})

    def _mcp_config(self, llm_url: str, model: str) -> str:
        config = set_model_fields(HERMES_TEMPLATE.read_text(encoding="utf-8"), model=model, base_url=llm_url)
        config = upsert_mcp_server(config, {
            "command": str(MCP_EXE).replace("\\", "/"), "args": [], "timeout": 4200,
            "env": {"REMAKE_HOME": str(self.home).replace("\\", "/"),
                    "OPENAI_BASE_URL": f"http://127.0.0.1:{self.painter.server_address[1]}/v1",
                    "OPENAI_API_KEY": "fake-painter-key", "REMAKE_IMAGE_MODEL": "fake-painter-model",
                    "REMAKE_GUIDE_IN_AGENTS": "1"}})
        config = set_discovery_timeout(config)
        config = set_tool_search_enabled(config)
        return config

    def start_llm(self) -> None:
        Handler.log_path = self.llm_log
        Handler.model_name = "fake-llm"
        Handler.status_only = False
        Handler.replay = Replay(None, "done")
        self.llm = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.llm.serve_forever, daemon=True).start()
        self.llm_url = f"http://127.0.0.1:{self.llm.server_address[1]}/v1"
        (self.hermes_home / "config.yaml").write_text(self._mcp_config(self.llm_url, "fake-llm"), encoding="utf-8")

    def set_script(self, entries: list[dict]) -> None:
        script = self.tmp / f"script_{time.monotonic_ns()}.jsonl"
        script.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries), encoding="utf-8")
        Handler.status_only = False
        Handler.replay = Replay(script, "done")

    def drive(self, prompt: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
        return run_edit_session(
            PROJECT_ROOT, self.sid, prompt, home=self.home, base_url=self.llm_url, api_key="fake-key",
            extra_env={"PATH": self.path_env, "PYTHONUTF8": "1"}, timeout=600, verbose=True, log=lambda _m: None)

    def kill_drive(self, prompt: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
        """首轮真 kill：Popen Hermes，待 take 落位（ingest/paint/submit 已完成）后 terminate。

        take 落位发生在 wait 工具执行期间（此刻无在途 LLM 请求），立即开 fake_llm 的
        status_only：其后所有应答强制 status 且不消费回放——进程凭 status 循环保活，
        回放零消耗，续跑轮脚本不会因 kill 竞态的在途弹出而错位。
        """
        workspace = prepare_workspace(PROJECT_ROOT, self.sid, log=lambda _m: None)
        command, env = hermes_chat_command(PROJECT_ROOT, prompt, workspace, base_url=self.llm_url,
                                           api_key="fake-key", home=self.home, verbose=True)
        run_env = {**os.environ, **env, "PATH": self.path_env, "PYTHONUTF8": "1"}
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=run_env)
        takes = self.home / "sessions" / self.sid / "case" / "takes"
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            if takes.exists() and any(takes.glob("w0_480_s0_*.mp4")):
                Handler.status_only = True  # 保活循环：强制 status 应答，回放零消耗
                time.sleep(1.0)  # 让保活循环至少跑一拍再 kill
                break
            time.sleep(0.2)
        alive = proc.poll() is None
        if os.name == "nt":
            # 杀整棵进程树：Hermes CLI 之下还有 MCP 等子进程
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            # gateway 是 CLI 另起的守护进程（不在 CLI 进程树内，--idle-exit 才退）：
            # 只杀 CLI 它会继续驱动首轮对话，与续跑轮交替发请求/抢回放——凭
            # gateway.lock 里的 pid 一并杀掉，才是真"kill agent 进程"
            try:
                lock = json.loads((self.hermes_home / "gateway.lock").read_text(encoding="utf-8"))
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(lock["pid"])], capture_output=True)
            except (OSError, ValueError, KeyError):
                pass
        else:
            proc.terminate()
            try:
                lock = json.loads((self.hermes_home / "gateway.lock").read_text(encoding="utf-8"))
                os.kill(lock["pid"], 9)
            except (OSError, ValueError, KeyError):
                pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        completed = subprocess.CompletedProcess(args=command, returncode=proc.returncode)
        completed._was_alive = alive  # 取证：terminate 时进程仍存活（真 kill）
        Handler.status_only = False  # 复位：类属性不跨轮/跨测试泄漏
        return completed, workspace, self.home / "sessions" / self.sid

    def requests(self) -> list[dict]:
        time.sleep(0.2)
        return [json.loads(line)["request"] for line in self.llm_log.read_text(encoding="utf-8").splitlines()]

    def close(self) -> None:
        if self.worker is not None:
            self.worker.terminate()
        if self.llm is not None:
            self.llm.shutdown()
        self.painter.shutdown()


@unittest.skipUnless(E2E_READY, "未装 MCP venv/Hermes 运行时/ffmpeg（阶段 3/7）")
class WatchdogE2eTests(unittest.TestCase):
    """agent 进程半途终止后重启续跑且不重做已完成步骤（零 GPU / 零 API 费）。"""

    def _round2(self, sid: str) -> list[dict]:
        """恢复轮：先 status 恢复现场，再 finish/save_result 交付（不重做 ingest/paint/submit）。

        kill 轮靠 status_only 保活（回放零消耗），strip 条目无论是否被在途弹出都不影响
        本轮对齐；半途收工轮 strip 已在首轮执行。两条 E2E 共用同一恢复脚本。
        """
        return [tool("status", {"sid": sid}),
                tool("finish", {"sid": sid, "picks": {"0": 0}, "canvas": "480"}),
                tool("save_result", {"sid": sid, "result": result_dict({"0": 0}, 1)}),
                {"content": "delivered"}]

    def _assert_resumed(self, result: WatchdogResult, harness: WatchdogHarness, sid: str,
                        prompt: str, resume: str) -> None:
        self.assertTrue(result.delivered, f"未交付：{result.prompts}")
        self.assertEqual(result.rounds, 2)
        self.assertEqual(result.prompts, [prompt, resume])
        requests = harness.requests()
        # 续跑轮主对话 = 含恢复提示的最后一个请求（按提示标记定位，不依赖日志末位：
        # kill 后若有首轮孤儿进程残留发请求，也不会误认作续跑轮）
        resume_requests = [r for r in requests
                           if "do NOT redo completed steps" in json.dumps(r.get("messages", []), ensure_ascii=False)]
        self.assertTrue(resume_requests, "续跑轮请求未携带恢复提示")
        main = resume_requests[-1]
        # 续跑轮只做 status → finish → save_result
        self.assertEqual(executed_names([main]),
                         [f"mcp__viggle_remake__{n}" for n in ("status", "finish", "save_result")])
        # 不重做已完成步骤：续跑轮没有 new_session/ingest/paint/submit/strip
        round2_names = executed_names([main])
        for redo in ("new_session", "ingest", "paint", "submit", "windows", "write_prompts", "set_prompt", "strip"):
            self.assertNotIn(f"mcp__viggle_remake__{redo}", round2_names)
        # 恢复提示实进续跑轮上下文（先 status、不重做）
        self.assertIn("status(sid) FIRST", json.dumps(main.get("messages", []), ensure_ascii=False))
        # 交付产物
        delivered = json.loads((result.session_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(delivered["status"], "done")
        self.assertTrue(Path(delivered["final"]).is_file(), delivered["final"])
        self.assertEqual(Path(delivered["final"]).name, "full_480.mp4")
        # 首轮已完成步骤的产物仍在（take 落位、关键帧已绘）——续跑凭这些现场，不重做
        self.assertTrue(any((harness.home / "sessions" / sid / "case" / "takes").glob("w0_480_s0_*.mp4")))
        self.assertTrue((harness.home / "sessions" / sid / "case" / "paint" / "g2" / "f60.png").is_file())

    def test_resume_after_premature_stop(self) -> None:
        sid = "m44-stop"
        with tempfile.TemporaryDirectory(prefix="m44-stop-", ignore_cleanup_errors=True) as temporary:
            harness = WatchdogHarness(Path(temporary), sid)
            harness.start_worker()
            harness.start_llm()
            prompt = edit_task_prompt(sid, str(harness.src), kind="swap", dur=10.0, cast={"P1": "Vela"}, budget=8)
            resume = resume_task_prompt(sid, str(harness.src), kind="swap", dur=10.0, cast={"P1": "Vela"}, budget=8)
            # 第 1 轮建到 strip（take 已落位）后模型半途收工（content-only，不交 result.json）
            round1 = base_entries(sid, str(harness.src)) + [{"content": "I will stop here without delivering."}]
            scripts = [round1, self._round2(sid)]
            calls = {"n": 0}

            def drive(current: str):
                calls["n"] += 1
                harness.set_script(scripts[calls["n"] - 1])
                return harness.drive(current)

            try:
                result = run_with_watchdog(
                    PROJECT_ROOT, sid, prompt, base_url=harness.llm_url, api_key="fake-key",
                    resume_prompt=resume, home=harness.home, max_rounds=3, drive=drive, log=lambda _m: None)
            finally:
                harness.close()
            self.assertEqual(calls["n"], 2)
            self._assert_resumed(result, harness, sid, prompt, resume)

    def test_resume_after_kill(self) -> None:
        sid = "m44-kill"
        with tempfile.TemporaryDirectory(prefix="m44-kill-", ignore_cleanup_errors=True) as temporary:
            harness = WatchdogHarness(Path(temporary), sid)
            harness.start_worker()
            harness.start_llm()
            prompt = edit_task_prompt(sid, str(harness.src), kind="swap", dur=10.0, cast={"P1": "Vela"}, budget=8)
            resume = resume_task_prompt(sid, str(harness.src), kind="swap", dur=10.0, cast={"P1": "Vela"}, budget=8)
            # 第 1 轮建到 take 落位后开 status_only 保活（回放零消耗），随即真 kill 进程
            round1 = base_entries(sid, str(harness.src))
            scripts = [round1, self._round2(sid)]
            calls = {"n": 0}
            killed = {}

            def drive(current: str):
                calls["n"] += 1
                harness.set_script(scripts[calls["n"] - 1])
                if calls["n"] == 1:
                    completed, workspace, session_dir = harness.kill_drive(current)
                    killed["returncode"] = completed.returncode
                    killed["alive"] = getattr(completed, "_was_alive", False)
                    return completed, workspace, session_dir
                return harness.drive(current)

            try:
                result = run_with_watchdog(
                    PROJECT_ROOT, sid, prompt, base_url=harness.llm_url, api_key="fake-key",
                    resume_prompt=resume, home=harness.home, max_rounds=3, drive=drive, log=lambda _m: None)
            finally:
                harness.close()
            self.assertEqual(calls["n"], 2)
            self.assertTrue(killed["alive"], "首轮 terminate 时进程应仍存活（真 kill）")
            self.assertNotEqual(killed["returncode"], 0, "被 kill 的进程退出码应非 0")
            self._assert_resumed(result, harness, sid, prompt, resume)


if __name__ == "__main__":
    unittest.main()