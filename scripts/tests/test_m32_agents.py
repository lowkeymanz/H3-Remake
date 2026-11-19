"""M3.2：AGENTS.md 规则注入——四件拼接落位、MCP 开关两态、fake_llm 回放、Hermes 上下文注入。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from app.agent.hermes import AgentError, hermes_python
from app.agent.workspace import (
    SOURCES,
    WorkspaceError,
    build_agents_md,
    mcp_data_dir,
    prepare_workspace,
)
from scripts.fake_llm import Handler, Replay

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HERMES_TEMPLATE = PROJECT_ROOT / "vendor/hermes/cli-config.yaml.example"
MCP_VENV_PY = PROJECT_ROOT / "vendor/viggle-remake/.venv/Scripts/python.exe"


def _hermes_python() -> Path | None:
    """阶段 7 落位的运行时解释器（$REMAKE_HOME/hermes/installs/.../venv）；未装则 None。"""
    try:
        return hermes_python(PROJECT_ROOT)
    except AgentError:
        return None


HERMES_PY = _hermes_python()


def make_data_dir(tmp: Path) -> Path:
    """四件套假源（含可断言标记文本），隔离于真实 vendor。"""
    data = tmp / "vendor/viggle-remake/mcp/viggle_remake_mcp/data"
    data.mkdir(parents=True)
    (data / "AGENTS.md").write_text("# Viggle Remake agent rules (the `viggle-remake` MCP)\nRULE-AGENT\n",
                                    encoding="utf-8")
    (data / "short_guide.md").write_text("RULE-GUIDE\n", encoding="utf-8")
    (data / "example_prompt.txt").write_text("RULE-SWAP-EXAMPLE\n", encoding="utf-8")
    (data / "example_edit_prompt.txt").write_text("RULE-EDIT-EXAMPLE\n", encoding="utf-8")
    return data


class WorkspaceTests(unittest.TestCase):
    def _root(self, tmp: Path) -> Path:
        root = tmp / "root"
        root.mkdir()
        make_data_dir(root)
        (root / "config.toml").write_text(
            f'[paths]\nremake_home = "{(tmp / "home").as_posix()}"\n', encoding="utf-8")
        return root

    def test_build_agents_md_matches_codex_exec_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(Path(temporary))
            text = build_agents_md(root)
            # 段序与文案与原项目 codex_exec.sh 逐字一致
            self.assertTrue(text.startswith("# Viggle Remake agent rules"))
            self.assertIn(
                "\n\n# Prompt-writing guide (write_prompts and cut_windows do not repeat it)"
                "\n\n=== GUIDE ===\nRULE-GUIDE\n", text)
            self.assertIn(
                "=== FORMAT EXAMPLE, swap sessions (a different edit of a different clip "
                "- copy its structure, tags and level of detail, not its content) ===\n"
                "RULE-SWAP-EXAMPLE\n", text)
            self.assertIn("=== FORMAT EXAMPLE, edit sessions (same) ===\nRULE-EDIT-EXAMPLE", text)
            self.assertLess(text.index("RULE-AGENT"), text.index("RULE-GUIDE"))
            self.assertLess(text.index("RULE-GUIDE"), text.index("RULE-SWAP-EXAMPLE"))
            self.assertLess(text.index("RULE-SWAP-EXAMPLE"), text.index("RULE-EDIT-EXAMPLE"))

    def test_missing_sources_report_stage2(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(Path(temporary))
            (mcp_data_dir(root) / "short_guide.md").unlink()
            with self.assertRaisesRegex(WorkspaceError, "阶段 2"):
                build_agents_md(root)

    def test_prepare_workspace_writes_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(Path(temporary))
            logs: list[str] = []
            workspace = prepare_workspace(root, "m32-ws", log=logs.append)
            self.assertEqual(workspace, Path(temporary) / "home/agents/m32-ws")
            target = workspace / "AGENTS.md"
            self.assertTrue(target.is_file())
            first = target.read_text(encoding="utf-8")
            prepare_workspace(root, "m32-ws", log=logs.append)
            self.assertEqual(first, target.read_text(encoding="utf-8"))  # 幂等
            self.assertTrue(any("无需重写" in line for line in logs))

    def test_prepare_workspace_rejects_bad_sid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(Path(temporary))
            with self.assertRaisesRegex(WorkspaceError, "会话 id"):
                prepare_workspace(root, "../escape", log=lambda _m: None)


@unittest.skipUnless(MCP_VENV_PY.is_file(), "未安装 MCP venv（阶段 3）；跳过真实开关断言")
class GuideFlagTests(unittest.TestCase):
    """真实 MCP 代码读 REMAKE_GUIDE_IN_AGENTS 的两态行为（prompts.GUIDE_AT）。"""

    def _guide_at(self, env_value: str | None) -> str:
        env = {**os.environ, "PYTHONUTF8": "1"}
        if env_value is None:
            env.pop("REMAKE_GUIDE_IN_AGENTS", None)
        else:
            env["REMAKE_GUIDE_IN_AGENTS"] = env_value
        result = subprocess.run(
            [str(MCP_VENV_PY), "-c", "from viggle_remake_mcp.prompts import GUIDE_AT; print(GUIDE_AT)"],
            capture_output=True, text=True, env=env, timeout=120, check=True)
        return result.stdout.strip()

    def test_flag_toggles_guide_location(self) -> None:
        self.assertEqual(self._guide_at("1"), "at the end of AGENTS.md")
        self.assertEqual(self._guide_at(None), "at the top of the session's first write_prompts result")


class FakeLlmTests(unittest.TestCase):
    """回放假件：非流式/流式响应与脚本顺序。"""

    def _serve(self, tmp: Path, replay: Replay) -> tuple[ThreadingHTTPServer, Path]:
        log = tmp / "llm.jsonl"
        Handler.replay = replay
        Handler.log_path = log
        Handler.model_name = "fake-llm"
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, log

    def _post(self, port: int, payload: dict) -> tuple[int, str]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()

    def test_non_stream_and_request_logging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            server, log = self._serve(tmp, Replay(None, "ACK"))
            try:
                status, body = self._post(server.server_address[1], {
                    "model": "fake-llm", "stream": False,
                    "messages": [{"role": "user", "content": "hello"}]})
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "ACK")
                logged = json.loads(log.read_text(encoding="utf-8").splitlines()[0])["request"]
                self.assertEqual(logged["messages"][0]["content"], "hello")
            finally:
                server.shutdown()

    def test_stream_sse_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            server, _log = self._serve(tmp, Replay(None, "OK"))
            try:
                _status, body = self._post(server.server_address[1], {
                    "model": "fake-llm", "stream": True,
                    "messages": [{"role": "user", "content": "hi"}]})
                self.assertIn('"delta":{"content":"OK"}', body.replace(" ", ""))
                self.assertTrue(body.rstrip().endswith("data: [DONE]"))
            finally:
                server.shutdown()

    def test_script_replay_in_order_with_tool_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            script = tmp / "script.jsonl"
            script.write_text(
                json.dumps({"content": "first"}) + "\n"
                + json.dumps({"content": "", "tool_calls": [
                    {"name": "new_session", "arguments": {"sid": "s1", "kind": "edit"}}]}) + "\n",
                encoding="utf-8")
            server, _log = self._serve(tmp, Replay(script, "fallback"))
            try:
                port = server.server_address[1]
                first = json.loads(self._post(port, {"model": "m", "messages": []})[1])
                self.assertEqual(first["choices"][0]["message"]["content"], "first")
                second = json.loads(self._post(port, {"model": "m", "messages": []})[1])
                call = second["choices"][0]["message"]["tool_calls"][0]
                self.assertEqual(call["function"]["name"], "new_session")
                self.assertEqual(json.loads(call["function"]["arguments"]), {"sid": "s1", "kind": "edit"})
                third = json.loads(self._post(port, {"model": "m", "messages": []})[1])
                self.assertEqual(third["choices"][0]["message"]["content"], "fallback")  # 耗尽回落
            finally:
                server.shutdown()


@unittest.skipUnless(HERMES_PY is not None and HERMES_PY.is_file() and HERMES_TEMPLATE.is_file(),
                    "未安装 Hermes 运行时（阶段 7）；跳过真实注入断言")
class HermesInjectionTests(unittest.TestCase):
    """Hermes × fake_llm × AGENTS.md 工作目录：规则文本实进 agent 上下文（零 API 费）。"""

    def test_agents_md_reaches_llm_context(self) -> None:
        with tempfile.TemporaryDirectory(prefix="m32-hermes-", ignore_cleanup_errors=True) as temporary:
            tmp = Path(temporary)
            root = tmp / "root"
            root.mkdir()
            make_data_dir(root)
            (root / "config.toml").write_text(
                f'[paths]\nremake_home = "{(tmp / "home").as_posix()}"\n', encoding="utf-8")
            logs: list[str] = []
            workspace = prepare_workspace(root, "m32-inject", log=logs.append)
            self.assertTrue((workspace / "AGENTS.md").is_file())

            hermes_home = tmp / "hermes-home"
            hermes_home.mkdir()
            (hermes_home / "config.yaml").write_text(
                HERMES_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")

            log = tmp / "llm.jsonl"
            Handler.replay = Replay(None, "OK")
            Handler.log_path = log
            Handler.model_name = "fake-llm"
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                env = {**os.environ, "HERMES_HOME": str(hermes_home), "PYTHONUTF8": "1"}
                command = [
                    str(HERMES_PY), "-m", "hermes_cli.main", "chat",
                    "-q", "Say OK and stop.", "--oneshot", "--yolo",
                    "--in", str(workspace),
                    "--provider", "custom",
                    "--base-url", f"http://127.0.0.1:{server.server_address[1]}/v1",
                    "--api-key", "fake-key",
                ]
                result = subprocess.run(command, capture_output=True, text=True,
                                        encoding="utf-8", errors="replace",
                                        env=env, timeout=180)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertTrue(log.is_file())
                requests = [json.loads(line)["request"]
                            for line in log.read_text(encoding="utf-8").splitlines()]
                self.assertTrue(requests, "fake_llm 未收到任何请求")
                context = json.dumps([r.get("messages", []) for r in requests], ensure_ascii=False)
                for marker in ("Viggle Remake agent rules", "=== GUIDE ===",
                               "RULE-AGENT", "RULE-GUIDE"):  # 规则原文与四件拼接段均入上下文
                    self.assertIn(marker, context)
            finally:
                server.shutdown()


if __name__ == "__main__":
    unittest.main()
