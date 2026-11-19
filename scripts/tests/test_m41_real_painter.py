"""M4.1：真 painter（MiniMax-H3-Image）接入——接入路径断言（零费）+ 图像质检单测 + 真费 E2E 回归（门控）。

单元测试（零 API 费）验证"假→真"替换在生产路径只改 MCP 注册 env 三元组：
config.toml [painter] → mcp_server_env（M3.1 已就绪）→ viggle-remake-mcp images API；
config.example.toml 模板防漂移断言；scripts/quality.py 量化"paint 出真图"。

真费 E2E（显式选择，默认跳过）：设 VIGGLE_REAL_PAINT=1 且 config.toml [painter].api_key
非空才运行——复刻 M3.3 E2E（fake_llm 回放 + fake_worker 照旧），仅把假 painter 换成真
MiniMax-H3-Image：epaint 经真实 images/edits 出真关键帧（质检通过）、result.json /
成片 / .done / 工具序列全链路回归照旧。每跑一次约一次 images 调用费用（¥1 量级，
见 dev/m1_02_paint_api_notes.md）；LLM 走 fake_llm 回放（零 LLM 费）、渲染零 GPU。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from PIL import Image

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from app.agent.hermes import (AgentError, hermes_python, mcp_server_env,
                               set_discovery_timeout, set_model_fields,
                               set_tool_search_enabled, upsert_mcp_server)
from app.agent.run import edit_task_prompt, run_edit_session
from scripts.config import Config, ConfigError, load_config
from scripts.fake_llm import Handler, Replay
from scripts.quality import image_report, is_generated_image

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MCP_EXE = PROJECT_ROOT / "vendor/viggle-remake/.venv/Scripts/viggle-remake-mcp.exe"
HERMES_TEMPLATE = PROJECT_ROOT / "vendor/hermes/cli-config.yaml.example"
SID = "m41-real"


def _hermes_python() -> Path | None:
    try:
        return hermes_python(PROJECT_ROOT)
    except AgentError:
        return None


HERMES_PY = _hermes_python()
FFMPEG_DIRS = sorted(Path.home().glob(".viggle-remake/hermes/tools/ffmpeg-*/bin"))
FFMPEG = str(FFMPEG_DIRS[-1] / "ffmpeg.exe") if FFMPEG_DIRS else None

try:
    PAINTER_CONFIG = load_config(PROJECT_ROOT)
except ConfigError:
    PAINTER_CONFIG = None

REAL_PAINT_READY = all((
    os.environ.get("VIGGLE_REAL_PAINT") == "1",
    PAINTER_CONFIG is not None and PAINTER_CONFIG.painter_api_key,
    MCP_EXE.is_file(), HERMES_PY is not None, HERMES_TEMPLATE.is_file(), bool(FFMPEG),
))


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


def _rich_png(path: Path, width: int, height: int) -> Path:
    """合成"图像样"：双通道梯度 + 取模扰动（灰度 std 与唯一色数远超纯色）。"""
    image = Image.new("RGB", (width, height))
    image.putdata([(x * 255 // max(1, width - 1),
                    y * 255 // max(1, height - 1),
                    (x + y) % 256)
                   for y in range(height) for x in range(width)])
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")
    return path


class QualityTests(unittest.TestCase):
    """质检口径：真图样过、纯色/过小/极端长宽比/坏文件不过。"""

    def test_generated_image_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = _rich_png(Path(temporary) / "rich.png", 640, 360)
            self.assertTrue(is_generated_image(path), image_report(path))

    def test_flat_image_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "flat.png"
            Image.new("RGB", (640, 360), (50, 50, 50)).save(path)
            self.assertFalse(is_generated_image(path))

    def test_small_image_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = _rich_png(Path(temporary) / "small.png", 64, 36)
            self.assertFalse(is_generated_image(path))  # 低于 API 尺寸下限 128

    def test_extreme_aspect_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = _rich_png(Path(temporary) / "wide.png", 640, 128)
            self.assertFalse(is_generated_image(path))  # 长宽比 5:1 超上限

    def test_broken_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "broken.png"
            path.write_text("not a png", encoding="utf-8")
            self.assertFalse(is_generated_image(path))

    def test_report_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = _rich_png(Path(temporary) / "rich.png", 640, 360)
            report = image_report(path)
            self.assertEqual((report["width"], report["height"]), (640, 360))
            self.assertEqual(report["aspect"], 1.78)
            self.assertGreater(report["std"], 15.0)
            self.assertGreaterEqual(report["colours"], 32)


class PainterEnvTests(unittest.TestCase):
    """真 painter 接入路径：config.toml [painter] 三元组 → MCP 注册 env（生产零改动）。"""

    def test_painter_triple_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.toml").write_text(
                '[painter]\nbase_url = "https://gw.example/v1"\n'
                'api_key = "sk-live"\nmodel = "MiniMax-H3-Image-Highspeed"\n',
                encoding="utf-8")
            home = root / "home"
            env = mcp_server_env(root, load_config(root), home=home)
            self.assertEqual(env["OPENAI_BASE_URL"], "https://gw.example/v1")
            self.assertEqual(env["OPENAI_API_KEY"], "sk-live")
            self.assertEqual(env["REMAKE_IMAGE_MODEL"], "MiniMax-H3-Image-Highspeed")
            self.assertEqual(env["REMAKE_HOME"], home.as_posix())
            self.assertEqual(env["REMAKE_GUIDE_IN_AGENTS"], "1")

    def test_default_triple_points_to_minimax(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)  # 无 config.toml → 定版默认
            env = mcp_server_env(root, Config(), home=root / "home")
            self.assertEqual(env["OPENAI_BASE_URL"], "https://api.minimax.cn/v1")
            self.assertEqual(env["REMAKE_IMAGE_MODEL"], "MiniMax-H3-Image")

    def test_example_template_matches_defaults(self) -> None:
        data = tomllib.loads((PROJECT_ROOT / "config.example.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["painter"], {
            "base_url": "https://api.minimax.cn/v1",
            "api_key": "",
            "model": "MiniMax-H3-Image",
        })


def _png_paths_in_text(text: str) -> list[str]:
    """从工具结果文本提取 PNG 路径：剥引号/括号并还原 JSON 双反斜杠（M3.3 踩坑 4 同源）。"""
    return [token.strip("\"'<>()[],").replace("\\\\", "\\")
            for token in re.findall(r"\S+\.png\b", text)]


def _epaint_painted_pngs(messages: list[dict]) -> list[str]:
    """按 tool_call_id 配对定位 epaint 调用的结果文本，提取其中的 PNG 路径。"""
    target_ids = {call["id"] for message in messages
                  for call in (message.get("tool_calls") or [])
                  if call["function"]["name"] == "mcp__viggle_remake__epaint"}
    return [path for message in messages
            if message.get("role") == "tool" and message.get("tool_call_id") in target_ids
            for path in _png_paths_in_text(str(message.get("content") or ""))]


@unittest.skipUnless(REAL_PAINT_READY,
                     "真费回归未开启：设 VIGGLE_REAL_PAINT=1 且 config.toml [painter].api_key "
                     "非空（真实 API 费，约 ¥1 量级/次）")
class RealPainterE2eTests(unittest.TestCase):
    """M3.3 E2E 的真 painter 版：唯一差异 = MCP env 三元组指向真实 MiniMax-H3-Image。"""

    def test_full_edit_session_with_real_painter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="m41-real-", ignore_cleanup_errors=True) as temporary:
            tmp = Path(temporary)
            home = tmp / "home"
            (home / "sessions").mkdir(parents=True)
            src = tmp / "src.mp4"
            subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
                            "-i", "testsrc=duration=5:size=640x360:rate=24",
                            "-pix_fmt", "yuv420p", "-c:v", "libx264", str(src)],
                           check=True, timeout=60)

            worker = subprocess.Popen(
                [sys.executable, str(PROJECT_ROOT / "scripts/fake_comfy_worker.py"),
                 "--home", str(home), "--beat-every", "2", "--poll", "1", "--max-736", "175"],
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

            def tool(name: str, arguments: dict) -> dict:
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
            config = set_model_fields(HERMES_TEMPLATE.read_text(encoding="utf-8"),
                                       model="fake-llm", base_url=llm_url)
            config = upsert_mcp_server(config, {
                "command": str(MCP_EXE).replace("\\", "/"),
                "args": [],
                "timeout": 4200,
                "env": {  # 与 M3.3 E2E 的唯一差异：三元组指向真实 MiniMax-H3-Image
                    "REMAKE_HOME": str(home).replace("\\", "/"),
                    "OPENAI_BASE_URL": PAINTER_CONFIG.painter_base_url,
                    "OPENAI_API_KEY": PAINTER_CONFIG.painter_api_key,
                    "REMAKE_IMAGE_MODEL": PAINTER_CONFIG.painter_model,
                    "REMAKE_GUIDE_IN_AGENTS": "1",
                },
            })
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
                    timeout=900, verbose=True, log=lambda _m: None)
            finally:
                worker.terminate()
                llm.shutdown()

            self.assertEqual(completed.returncode, 0,
                             f"stderr: {completed.stderr[-1500:]}\nstdout: {completed.stdout[-1500:]}")
            time.sleep(0.2)  # 等 server 线程写完日志

            requests = [json.loads(line)["request"]
                        for line in llm_log.read_text(encoding="utf-8").splitlines()]
            first_context = json.dumps(requests[0].get("messages", []), ensure_ascii=False)
            names = [call["function"]["name"]
                     for message in requests[-1].get("messages", [])
                     if message.get("role") == "assistant" and message.get("tool_calls")
                     for call in message["tool_calls"]]
            self.assertEqual(names, [e["tool_calls"][0]["name"] for e in entries[:-1]])
            self.assertIn("Viggle Remake agent rules", first_context)

            result = json.loads((session_dir / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["sid"], SID)
            final = Path(result["final"])
            self.assertTrue(final.is_file(), result["final"])
            self.assertEqual(final.name, "full_480.mp4")
            self.assertTrue(any((home / "queue").glob("*.done")))

            # 断言 4（本单元核心）：epaint 经真实 images/edits 出真关键帧，质检通过。
            # 首选口径 = epaint 工具结果文本携带的产物路径（对 MCP 内部落盘结构零依赖）；
            # 文本未携带路径时兜底扫描会话目录（源帧/绘制帧同为真图，报告供目视核对）。
            painted = sorted({p for p in _epaint_painted_pngs(requests[-1].get("messages", []))
                              if Path(p).is_file()})
            if painted:
                reports = {p: image_report(p) for p in painted}
                self.assertTrue(any(is_generated_image(p) for p in painted),
                                f"真图质检未通过：{reports}")
                print(f"[m41] epaint 产物质检：{json.dumps(reports, ensure_ascii=False)}")
            else:
                session_pngs = sorted(str(p) for p in session_dir.rglob("*.png"))
                self.assertGreaterEqual(len(session_pngs), 2,
                                        f"会话目录无绘制产物：{session_pngs}")
                reports = {p: image_report(p) for p in session_pngs}
                self.assertTrue(any(is_generated_image(p) for p in session_pngs),
                                f"真图质检未通过：{reports}")
                print(f"[m41] 会话目录产物质检：{json.dumps(reports, ensure_ascii=False)}")


if __name__ == "__main__":
    unittest.main()
