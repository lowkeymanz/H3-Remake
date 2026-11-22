"""M3.3：edit 全流程编排——任务提示组装、fake_painter、回填占位符、全链路 E2E。

E2E = Hermes × viggle-remake-mcp × fake_worker × fake_painter × fake_llm 回放，
new_session → save_result 全链路真实执行，工具调用序列与产物双断言。
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from app.agent.hermes import AgentError, hermes_python
from app.agent.run import edit_task_prompt, hermes_chat_command, library_notes, run_edit_session
from scripts.config import resolve_remake_home
from scripts.installer import venv_python
from scripts.fake_llm import Handler, Replay
from scripts.fake_painter import Handler as PainterHandler, make_png

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MCP_EXE = venv_python(PROJECT_ROOT / "vendor/viggle-remake/.venv").parent / (
    "viggle-remake-mcp.exe" if sys.platform == "win32" else "viggle-remake-mcp")
HERMES_TEMPLATE = PROJECT_ROOT / "vendor/hermes/cli-config.yaml.example"
SID = "m33-e2e"


def _hermes_python() -> Path | None:
    try:
        return hermes_python(PROJECT_ROOT)
    except AgentError:
        return None


HERMES_PY = _hermes_python()
# 双平台口径：按宿主平台取可执行名、文件存在才算数，回落系统 PATH
FFMPEG_DIRS = sorted(resolve_remake_home(PROJECT_ROOT).glob("hermes/tools/ffmpeg-*/bin"))
_FFMPEG_NAME = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
FFMPEG = next((str(d / _FFMPEG_NAME) for d in reversed(FFMPEG_DIRS)
               if (d / _FFMPEG_NAME).is_file()), None) or shutil.which("ffmpeg")

EDIT_PROMPT = """subject_definitions:
<Video 1> is the source video for the target video edit.
<Picture 1> is a frame of the target video, exactly as the target video shows it at that moment.
<Subject 1> is the man in the centre of the frame.
<Subject 2> is the moving test pattern background.

summary:
[video editing] The target video is an edited version of <Video 1>. The red ball <Subject 1> holds is removed for the entire clip. His pose, the camera moves, the timing of every cut and the background pattern are exactly as in <Video 1>.

retention_analysis:
<Video 1> (source video editing): fully_preserved - the man's pose, the pattern background, the camera moves and the timing are kept.
<Picture 1> (target keyframe): fully_preserved - the frame as the target video shows it, his open empty hand visible.
<Subject 1>: fully_preserved - the man stays exactly as in the source, only his hand is open with nothing in it.
<Subject 2>: fully_preserved - the background pattern unchanged behind where the ball was.

detailed_description:
[Shot 1] At 00:00.000 the clip opens on the man in the centre holding nothing, his right hand open, the test pattern moving behind him exactly as in <Video 1>; he stays centred, the colours and grain of the source kept, no ball anywhere in frame.

overall_soundscape:
As in the source video: the original soundtrack is kept unchanged.

non_diegetic_music:
N/A
"""


class PromptTests(unittest.TestCase):
    def test_edit_task_prompt_fields(self) -> None:
        prompt = edit_task_prompt(SID, "D:/clips/a.mp4", start=1.0, dur=5.0,
                                   edit="remove the ball", budget=4)
        self.assertIn(f"Session id: {SID}. Kind: edit.", prompt)
        self.assertIn("Source clip: D:/clips/a.mp4. Span: [1, 6) (seconds).", prompt)
        self.assertIn("Edit: remove the ball", prompt)
        self.assertIn("Image-call budget for this session: 4.", prompt)

    def test_swap_cast_and_defaults(self) -> None:
        prompt = edit_task_prompt(SID, "a.mp4", kind="swap", dur=8.0,
                                   cast={"P1": "Vela", "P2": "none"})
        self.assertIn("Kind: swap.", prompt)
        self.assertIn("Cast: P1 -> Vela, P2 -> none", prompt)
        self.assertNotIn("budget", prompt)
        self.assertNotIn("Edit:", prompt)

    def test_library_notes_name_lib_sheet_and_description(self) -> None:
        """库中已有的 cast 目标逐角色点名 sheet(source="lib") 与库描述，防 agent 自造角色。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            chars = Path(temporary)
            (chars / "Pete_sheet.png").write_bytes(b"sheet")
            (chars / "Pete_photo.png").write_bytes(b"photo")
            (chars / "lib_chars.json").write_text(json.dumps(
                {"chars": {"Pete": "a pineapple mascot"}, "outfits": {"Pete": "yellow bodysuit"}}),
                encoding="utf-8")
            notes = library_notes({"P1": "Pete", "P2": "Pete", "P3": "Absent"}, chars)
            self.assertEqual(len(notes), 1)  # 去重；不在库的目标不附注
            self.assertIn('sheet(source="lib") for Pete', notes[0])
            self.assertIn("do NOT invent or redraw", notes[0])
            self.assertIn("Pete_sheet.png, photo", notes[0])
            self.assertIn("a pineapple mascot", notes[0])
            self.assertIn("yellow bodysuit", notes[0])

    def test_swap_prompt_carries_library_notes(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            chars = Path(temporary)
            (chars / "Vela_sheet.png").write_bytes(b"sheet")
            prompt = edit_task_prompt(SID, "a.mp4", kind="swap", cast={"P1": "Vela"}, chars_dir=chars)
            self.assertIn('sheet(source="lib") for Vela', prompt)
            without = edit_task_prompt(SID, "a.mp4", kind="swap", cast={"P1": "Vela"})
            self.assertNotIn("Character library:", without)  # 未给 chars_dir 口径不变


@unittest.skipUnless(HERMES_PY is not None, "未安装 Hermes 运行时（阶段 7）")
class ChatCommandTests(unittest.TestCase):
    def test_hermes_chat_command_assembly(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            workspace = Path(temporary) / "ws"
            workspace.mkdir()
            command, env = hermes_chat_command(PROJECT_ROOT, "do it", workspace,
                                               base_url="http://127.0.0.1:9/v1", api_key="k")
            self.assertEqual(command[:2], [str(HERMES_PY), "-m", "hermes_cli.main"][:2])
            tail = command[command.index("chat"):]
            self.assertEqual(tail[:3], ["chat", "-q", "do it"])
            for flag, value in (("--oneshot", None), ("--yolo", None), ("--in", str(workspace)),
                                ("--provider", "custom"),
                                ("--base-url", "http://127.0.0.1:9/v1"),
                                ("--api-key", "k")):
                self.assertIn(flag, tail)
                if value:
                    self.assertEqual(tail[tail.index(flag) + 1], value)
            self.assertIn("HERMES_HOME", env)


class FakePainterTests(unittest.TestCase):
    def _serve(self, tmp: Path) -> tuple[ThreadingHTTPServer, Path]:
        log = tmp / "painter.jsonl"
        PainterHandler.log_path = log
        PainterHandler.colour = (96, 130, 80)
        server = ThreadingHTTPServer(("127.0.0.1", 0), PainterHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, log

    def test_edit_request_returns_png_and_logs(self) -> None:
        from openai import OpenAI
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            tmp = Path(temporary)
            server, log = self._serve(tmp)
            try:
                source = tmp / "f0.png"
                Image.new("RGB", (64, 36), (10, 10, 10)).save(source)
                client = OpenAI(api_key="fake", base_url=f"http://127.0.0.1:{server.server_address[1]}/v1")
                with source.open("rb") as handle:
                    response = client.images.edit(model="fake-painter-model", image=[handle],
                                                  prompt="remove the ball", size="64x36",
                                                  response_format="b64_json")
                image = Image.open(io.BytesIO(base64.b64decode(response.data[0].b64_json)))
                self.assertEqual(image.size, (64, 36))
                time.sleep(0.2)  # 等 handler 线程写完日志
                entry = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
                self.assertEqual(entry["endpoint"], "edits")
                self.assertEqual(entry["fields"]["prompt"], "remove the ball")
                self.assertEqual(entry["fields"]["size"], "64x36")
                self.assertEqual(entry["images"], ["f0.png"])
            finally:
                server.shutdown()

    def test_make_png_size_fallback(self) -> None:
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(make_png("32x16", (1, 2, 3))))).size, (32, 16))
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(make_png("bad", (1, 2, 3))))).size, (1024, 1024))


class ReplayFillTests(unittest.TestCase):
    """回填占位符 "${re:<pattern>}"：从工具结果文本取值（save_result 的 final 路径等）。"""

    def _message(self, arguments: dict, messages: list[dict]) -> dict:
        replay = Replay(None, "OK")
        replay.entries = [{"content": "", "tool_calls": [{"name": "save_result", "arguments": arguments}]}]
        return replay.message("m", {"messages": messages})["choices"][0]["message"]

    def test_fill_from_tool_text(self) -> None:
        request_messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "finish", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1",
             "content": r"C:\tmp\home\sessions\m33-e2e\case\final\full_480.mp4 (832x468, 120 frames)"},
        ]
        message = self._message({"final": r"${re:\S+\.mp4}"}, request_messages)
        self.assertEqual(json.loads(message["tool_calls"][0]["function"]["arguments"])["final"],
                         r"C:\tmp\home\sessions\m33-e2e\case\final\full_480.mp4")

    def test_fill_from_json_escaped_tool_text(self) -> None:
        # Hermes 把 MCP 工具结果包成 <untrusted_tool_result>{"result": "<JSON 转义文本>"}
        request_messages = [
            {"role": "tool", "tool_call_id": "c1", "content":
                '{"result": "C:\\\\tmp\\\\home\\\\sessions\\\\m33-e2e\\\\case\\\\final\\\\'
                'full_480.mp4 (832x468, 120 frames) kept: w0 s0"}'},
        ]
        message = self._message({"final": r"${re:\S+\.mp4}"}, request_messages)
        self.assertEqual(json.loads(message["tool_calls"][0]["function"]["arguments"])["final"],
                         r"C:\tmp\home\sessions\m33-e2e\case\final\full_480.mp4")

    def test_fill_unmatched_and_plain(self) -> None:
        request_messages = [{"role": "tool", "content": "nothing here"}]
        message = self._message({"a": "${re:\\d+}", "b": "plain"}, request_messages)
        arguments = json.loads(message["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(arguments["a"], "${re:\\d+}")  # 未命中保留原样
        self.assertEqual(arguments["b"], "plain")


@unittest.skipUnless(MCP_EXE.is_file() and HERMES_PY is not None and HERMES_TEMPLATE.is_file()
                     and FFMPEG, "未装 MCP venv/Hermes 运行时/ffmpeg（阶段 3/7）")
class E2eEditTests(unittest.TestCase):
    """全链路：mock LLM 按序回放工具调用 → Hermes 真实执行 → 产物与序列双断言。"""

    def test_full_edit_session(self) -> None:
        with tempfile.TemporaryDirectory(prefix="m33-e2e-", ignore_cleanup_errors=True) as temporary:
            tmp = Path(temporary)
            home = tmp / "home"
            (home / "sessions").mkdir(parents=True)
            src = tmp / "src.mp4"
            subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
                            "-i", "testsrc=duration=5:size=640x360:rate=24",
                            "-pix_fmt", "yuv420p", "-c:v", "libx264", str(src)],
                           check=True, timeout=60)

            painter_log = tmp / "painter.jsonl"
            PainterHandler.log_path = painter_log
            PainterHandler.colour = (96, 130, 80)
            painter = ThreadingHTTPServer(("127.0.0.1", 0), PainterHandler)
            threading.Thread(target=painter.serve_forever, daemon=True).start()

            worker = subprocess.Popen(
                [sys.executable, str(PROJECT_ROOT / "scripts/fake_comfy_worker.py"),
                 "--home", str(home), "--beat-every", "2", "--poll", "1", "--max-736", "175"],
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

            def tool(name: str, arguments: dict) -> dict:
                # Hermes 以命名空间名注册 MCP 工具：mcp__<server>__<tool>
                return {"content": "", "tool_calls": [
                    {"name": f"mcp__viggle_remake__{name}", "arguments": arguments}]}

            script = tmp / "script.jsonl"
            entries = [
                tool("new_session", {"sid": SID, "kind": "edit"}),
                tool("peek", {"sid": SID, "src": str(src), "start": 0.0, "dur": 5.0}),
                tool("ingest", {"sid": SID, "src": str(src), "start": 0.0, "dur": 5.0}),
                tool("frames", {"sid": SID, "frames": [62]}),
                tool("save_case", {
                    "sid": SID,
                    "plan_grid": {"people": [
                        {"id": "P1", "desc": "the man in the centre", "replace_with": "none"},
                        {"id": "X1", "desc": "the red ball", "replace_with": "NoBall"}],
                        "shots": [{"start": 0, "end": 124, "people": ["P1", "X1"],
                                   "desc": "a man with a red ball on a moving test pattern"}]},
                    "case": {"chars": {"NoBall": "removed: no ball anywhere"},
                             "outfits": {"NoBall": "none"},
                             "edit": "remove the red ball completely"},
                    "where": {"62": {"P1": "centre, facing camera", "X1": "in his right hand"}}}),
                tool("windows", {"sid": SID}),
                tool("epaint", {"sid": SID, "frame": 62,
                                "edit": "Remove the red ball completely: his hand is open and empty, "
                                        "the pattern behind where it was is visible."}),
                tool("windows", {"sid": SID}),
                tool("cut_windows", {"sid": SID}),
                tool("edit_prompt", {"sid": SID, "window": 0, "text": EDIT_PROMPT}),
                tool("submit", {"sid": SID, "seeds": [0], "canvas": "480"}),
                tool("wait", {"sid": SID, "max_s": 60}),
                tool("strip", {"sid": SID, "canvas": "480"}),
                tool("finish", {"sid": SID, "picks": {"0": 0}, "canvas": "480"}),
                tool("save_result", {"sid": SID, "result": {
                    "status": "done", "final": r"${re:\S+\.mp4}", "seconds": 5.0,
                    "cast": {"X1": "NoBall"}, "caption": "the red ball removed",
                    "picks": {"0": 0}, "windows": [{"w": 0, "grade": "OK", "note": "clean"}],
                    "overall": "OK", "image_calls": 1, "changed": "", "problems": ""}}),
                {"content": "delivered"},
            ]
            script.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries),
                              encoding="utf-8")

            llm_log = tmp / "llm.jsonl"
            Handler.replay = Replay(script, "done")
            Handler.log_path = llm_log
            Handler.model_name = "fake-llm"
            llm = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=llm.serve_forever, daemon=True).start()

            hermes_home = home / "hermes"
            hermes_home.mkdir()
            llm_url = f"http://127.0.0.1:{llm.server_address[1]}/v1"
            from app.agent.hermes import (set_discovery_timeout, set_model_fields,
                                          set_tool_search_enabled, upsert_mcp_server)
            config = set_model_fields(HERMES_TEMPLATE.read_text(encoding="utf-8"),
                                       model="fake-llm", base_url=llm_url)
            config = upsert_mcp_server(config, {
                "command": str(MCP_EXE).replace("\\", "/"),
                "args": [],
                "timeout": 4200,
                "env": {
                    "REMAKE_HOME": str(home).replace("\\", "/"),
                    "OPENAI_BASE_URL": f"http://127.0.0.1:{painter.server_address[1]}/v1",
                    "OPENAI_API_KEY": "fake-painter-key",
                    "REMAKE_IMAGE_MODEL": "fake-painter-model",
                    "REMAKE_GUIDE_IN_AGENTS": "1",
                },
            })
            # M3.3 修复：MCP 冷启动握手 8-10s，默认发现时限 1.5s 不够（首轮 tools 缺
            # MCP 工具 → "Unknown tool"）；tool_search 关闭后工具直出（命名空间名
            # mcp__viggle_remake__*），回放与真 LLM 路径同构。
            config = set_discovery_timeout(config)
            config = set_tool_search_enabled(config)
            (hermes_home / "config.yaml").write_text(config, encoding="utf-8")

            prompt = edit_task_prompt(SID, str(src), dur=5.0, edit="remove the red ball",
                                      budget=4)
            path_env = f"{Path(FFMPEG).parent};{os.environ['PATH']}"
            try:
                completed, _workspace, session_dir = run_edit_session(
                    PROJECT_ROOT, SID, prompt, home=home,
                    base_url=llm_url, api_key="fake-key",
                    extra_env={"PATH": path_env, "PYTHONUTF8": "1"},
                    timeout=480, verbose=True, log=lambda _m: None)
            finally:
                worker.terminate()
                llm.shutdown()
                painter.shutdown()

            self.assertEqual(completed.returncode, 0,
                             f"stderr: {completed.stderr[-1500:]}\nstdout: {completed.stdout[-1500:]}")
            time.sleep(0.2)  # 等 server 线程写完日志

            # 断言 1：工具调用序列符合预期。Hermes 每轮请求携带全量历史，取末轮
            # （含全部 assistant tool_calls）按序收集即可。
            requests = [json.loads(line)["request"]
                        for line in llm_log.read_text(encoding="utf-8").splitlines()]
            first_context = json.dumps(requests[0].get("messages", []), ensure_ascii=False)
            names = [call["function"]["name"]
                     for message in requests[-1].get("messages", [])
                     if message.get("role") == "assistant" and message.get("tool_calls")
                     for call in message["tool_calls"]]
            self.assertEqual(names, [e["tool_calls"][0]["name"] for e in entries[:-1]])
            self.assertIn("Viggle Remake agent rules", first_context)  # 规则注入在编排链路中生效

            # 断言 2：会话产物（new_session → save_result 全链路副作用）
            result = json.loads((session_dir / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["sid"], SID)
            final = Path(result["final"])
            self.assertTrue(final.is_file(), result["final"])
            self.assertEqual(final.name, "full_480.mp4")
            self.assertTrue(any((home / "queue").glob("*.done")))  # fake_worker 真实渲染过

            # 断言 3：假 painter 收到 epaint 的 images/edits 请求（MCP 会把编辑指令包进
            # 自己的 prompt 模板，故按大小写不敏感匹配）
            painter_entries = [json.loads(l) for l in painter_log.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(e["endpoint"] == "edits"
                                and "remove the red ball" in e["fields"].get("prompt", "").lower()
                                for e in painter_entries), painter_entries)


if __name__ == "__main__":
    unittest.main()
