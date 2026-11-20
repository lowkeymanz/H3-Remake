"""M4.3：swap 模式 + 审查-重试循环——缺陷样片构造单测 + swap 全链路 E2E + 三条审查-重试 E2E + 真 LLM 门控。

零 GPU / 零 API 费回归（fake_llm 回放 × fake_painter × fake_comfy_worker × viggle-remake-mcp × Hermes）：
- 单测：scripts/defects.py 四类缺陷构造（边缘坏条 / 切点提前 / 延后 / 丢切）保帧数、可被量测，
  DefectPlan 的 occurrence 机制（首 take 带缺陷、修复后重渲干净），scripts/quality.py 视频边缘坏条判定；
- clean swap E2E：new_session → save_result 全链路，工具序列与产物双断言（对齐 M3.3 edit E2E）；
- 三条审查-重试 E2E：fake_worker --defect-plan 向首 take 注入已知缺陷 → MCP strip 量测出缺陷 →
  回放脚本执行 AGENTS.md 对应修复分支（重绘 force / seed 1 / pin_before_cuts）→ 重渲干净 → 交付。
  断言 strip 确实量测到缺陷（修复动作有明确判定目标）且修复分支工具成功执行、成片干净。

真 LLM 门控（显式选择，默认跳过）：设 VIGGLE_REAL_LLM=1 且 config.toml [llm].api_key 非空才运行——
fake 预建带边缘坏条缺陷的会话（到首次 strip），再换真 qwen3.7-plus 自主审查-修复：断言真 LLM 正确
触发重绘修复动作（paint force=true 留下 old/ 重绘证据）并交付。painter 仍走假件（零图像费）、渲染零 GPU，
仅 LLM token 费。每跑一次约一次自主审查-修复的 token 费（见 dev/m1_03_llm_notes.md）。
"""

from __future__ import annotations

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

from app.agent.hermes import (AgentError, hermes_python, set_discovery_timeout,
                               set_model_fields, set_tool_search_enabled, upsert_mcp_server)
from app.agent.run import edit_task_prompt, run_edit_session
from scripts import defects
from scripts.config import ConfigError, load_config, resolve_remake_home
from scripts.fake_llm import Handler, Replay
from scripts.fake_painter import Handler as PainterHandler
from scripts.quality import video_edge_bands

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MCP_EXE = PROJECT_ROOT / "vendor/viggle-remake/.venv/Scripts/viggle-remake-mcp.exe"
HERMES_TEMPLATE = PROJECT_ROOT / "vendor/hermes/cli-config.yaml.example"


def _hermes_python() -> Path | None:
    try:
        return hermes_python(PROJECT_ROOT)
    except AgentError:
        return None


HERMES_PY = _hermes_python()
# 双平台口径：按宿主平台取可执行名、文件存在才算数，回落系统 PATH
FFMPEG_DIRS = sorted(resolve_remake_home(PROJECT_ROOT).glob("hermes/tools/ffmpeg-*/bin"))
_FFMPEG_NAME = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
_FFPROBE_NAME = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
FFMPEG = next((str(d / _FFMPEG_NAME) for d in reversed(FFMPEG_DIRS)
               if (d / _FFMPEG_NAME).is_file()), None) or shutil.which("ffmpeg")
FFPROBE = next((str(d / _FFPROBE_NAME) for d in reversed(FFMPEG_DIRS)
                if (d / _FFPROBE_NAME).is_file()), None) or shutil.which("ffprobe")

E2E_READY = MCP_EXE.is_file() and HERMES_PY is not None and HERMES_TEMPLATE.is_file() and bool(FFMPEG)

try:
    LLM_CONFIG = load_config(PROJECT_ROOT)
except ConfigError:
    LLM_CONFIG = None

REAL_LLM_READY = all((
    os.environ.get("VIGGLE_REAL_LLM") == "1",
    LLM_CONFIG is not None and LLM_CONFIG.llm_api_key,
    E2E_READY,
))

# ── swap 会话的固定案卷（两镜头源，切点 f120，单窗覆盖 [0,243)）─────────────────
PLAN = {"people": [{"id": "P1", "desc": "the person in the centre of the frame", "replace_with": "Vela"}],
        "shots": [{"start": 0, "end": 120, "people": ["P1"], "desc": "a person centred on a moving test pattern"},
                  {"start": 120, "end": 243, "people": ["P1"], "desc": "the same person centred on a second pattern"}]}
CASE = {"chars": {"Vela": "Vela, a woman in her thirties, athletic build, short dark hair, warm brown eyes"},
        "outfits": {"Vela": "teal flight jacket, grey turtleneck, cargo trousers, brown boots"}}
WHERE = {"60": {"P1": "centre, facing camera, full figure (frame 60)"},
         "180": {"P1": "centre, facing camera, full figure (frame 180)"}}

SWAP_PROMPT = """subject_definitions:
<Video 1> is the source video for the target video swap.
<Picture 1> is a frame of the target video, exactly as the target video shows it at that moment.
<Picture 2> is a frame of the target video, exactly as the target video shows it at that moment.
<Subject 1> is Vela, the woman who replaces the person in the centre of the frame.

summary:
[video swap] The target video is <Video 1> with the person in the centre replaced by Vela <Subject 1>. The camera, the moving test pattern background and the timing of every cut are exactly as in <Video 1>.

retention_analysis:
<Video 1> (source video swap): fully_preserved - the camera, the pattern background, the cut at 00:05.000 and the timing are kept.
<Picture 1> (appears in [Shot 1]): reference - a frame of the target video showing Vela centred at 00:02.500.
<Picture 2> (appears in [Shot 2]): reference - a frame of the target video showing Vela centred at 00:07.500.
<Subject 1>: fully_preserved - Vela keeps her teal flight jacket, grey turtleneck, cargo trousers and brown boots in every shot.

detailed_description:
[Shot 1] At 00:00.000 the clip opens on Vela <Subject 1> in the centre, the test pattern moving behind her exactly as in <Video 1>; she stays centred through the first shot.
[Shot 2] At 00:05.000 the cut lands on the second shot, Vela <Subject 1> still centred, the second pattern continuing behind her as in <Video 1>.

overall_soundscape:
As in the source video: the original soundtrack is kept unchanged.

non_diegetic_music:
N/A
"""

# pin_before_cuts(0,[120]) 后 refs=[114,180]：<Picture 1> 改绑 f114（00:04.750，仍在 [Shot 1]）
PINNED_PROMPT = SWAP_PROMPT.replace("showing Vela centred at 00:02.500", "showing Vela centred at 00:04.750")

EDGE_PLAN = [{"mode": "swap", "seed": 0, "tier": 480, "occurrence": 1,
              "defect": {"kind": "edge_band", "edge": "bottom", "first": 150, "last": 179, "thickness": 24}}]
LATE_PLAN = [{"mode": "swap", "seed": 0, "tier": 480, "occurrence": 1, "defect": {"kind": "cut_late", "frames": 12}}]
EARLY_PLAN = [{"mode": "swap", "seed": 0, "tier": 480, "occurrence": 1, "defect": {"kind": "cut_early", "frames": 12}}]


def tool(name: str, arguments: dict) -> dict:
    """Hermes 以命名空间名注册 MCP 工具：mcp__<server>__<tool>。"""
    return {"content": "", "tool_calls": [{"name": f"mcp__viggle_remake__{name}", "arguments": arguments}]}


def result_dict(picks: dict, image_calls: int) -> dict:
    return {"status": "done", "final": r"${re:\S+\.mp4}", "seconds": 10.0,
            "cast": {"P1": "Vela"}, "caption": "the person replaced by Vela",
            "picks": picks, "windows": [{"w": 0, "grade": "OK", "note": "clean"}],
            "overall": "OK", "image_calls": image_calls, "changed": "", "problems": ""}


def base_entries(sid: str, src: str) -> list[dict]:
    """swap 基础链路（到首次 strip）：new_session → ingest → 案卷 → sheet → paint → prompts → submit → strip。"""
    return [
        tool("new_session", {"sid": sid, "kind": "swap"}),
        tool("peek", {"sid": sid, "src": src, "start": 0.0, "dur": 10.0}),
        tool("ingest", {"sid": sid, "src": src, "start": 0.0, "dur": 10.0}),
        tool("frames", {"sid": sid, "frames": [60, 180]}),
        tool("save_case", {"sid": sid, "plan_grid": PLAN, "case": CASE, "where": WHERE, "cuts": [120]}),
        tool("sheet", {"sid": sid, "name": "Vela", "source": "lib"}),
        tool("windows", {"sid": sid, "canvas": "480"}),
        tool("paint", {"sid": sid, "frames": [60, 180], "quality": "low"}),
        tool("windows", {"sid": sid, "canvas": "480"}),
        tool("write_prompts", {"sid": sid}),
        tool("set_prompt", {"sid": sid, "window": 0, "text": SWAP_PROMPT}),
        tool("submit", {"sid": sid, "seeds": [0], "canvas": "480"}),
        tool("wait", {"sid": sid, "max_s": 120}),
        tool("strip", {"sid": sid, "canvas": "480"}),
    ]


def tool_results(requests: list[dict], name: str) -> list[str]:
    """末轮请求里某工具的全部结果文本（按 tool_call_id 配对，调用序）。"""
    messages = requests[-1].get("messages", [])
    ids = {call["id"] for message in messages for call in (message.get("tool_calls") or [])
           if call["function"]["name"] == f"mcp__viggle_remake__{name}"}
    return [str(message.get("content") or "") for message in messages
            if message.get("role") == "tool" and message.get("tool_call_id") in ids]


def tool_args(requests: list[dict], name: str) -> list[dict]:
    """末轮请求里某工具的全部调用参数（调用序）。"""
    messages = requests[-1].get("messages", [])
    return [json.loads(call["function"]["arguments"]) for message in messages
            if message.get("role") == "assistant" for call in (message.get("tool_calls") or [])
            if call["function"]["name"] == f"mcp__viggle_remake__{name}"]


def executed_names(requests: list[dict]) -> list[str]:
    return [call["function"]["name"] for message in requests[-1].get("messages", [])
            if message.get("role") == "assistant" and message.get("tool_calls")
            for call in message["tool_calls"]]


def _rich_png(path: Path, width: int, height: int) -> Path:
    """合成"图像样"角色表：双通道梯度 + 取模扰动（非纯色，供 sheet lib 拷贝）。"""
    image = Image.new("RGB", (width, height))
    image.putdata([(x * 255 // max(1, width - 1), y * 255 // max(1, height - 1), (x + y) % 256)
                   for y in range(height) for x in range(width)])
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")
    return path


def _make_source(path: Path) -> None:
    """两段 5s testsrc/testsrc2 拼接 → 240 帧 640x360 24fps，硬切点在 f120（无音轨）。"""
    subprocess.run([FFMPEG, "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=duration=5:size=640x360:rate=24",
                    "-f", "lavfi", "-i", "testsrc2=duration=5:size=640x360:rate=24",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", str(path)], check=True, timeout=120)


@unittest.skipUnless(FFMPEG, "未装 ffmpeg（缺陷构造/量测依赖）")
class DefectTests(unittest.TestCase):
    """缺陷样片构造与判定（零费）：四类缺陷保帧数、可被量测；DefectPlan occurrence；quality 视频坏条。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._ffmpeg, cls._ffprobe = defects.FFMPEG, defects.FFPROBE
        defects.FFMPEG, defects.FFPROBE = FFMPEG, FFPROBE  # 把 Hermes tools 的 ffmpeg 指给构造/量测

    @classmethod
    def tearDownClass(cls) -> None:
        defects.FFMPEG, defects.FFPROBE = cls._ffmpeg, cls._ffprobe

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.clip = self.tmp / "clip.mp4"
        _make_source(self.clip)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _take(self) -> Path:
        take = self.tmp / "take.mp4"
        take.write_bytes(self.clip.read_bytes())
        return take

    def test_detect_cuts_finds_the_hard_cut(self) -> None:
        self.assertEqual(defects.detect_cuts(self.clip), [120])

    def test_edge_band_injects_and_quality_sees_it(self) -> None:
        take = self._take()
        defects.edge_band(take, edge="bottom", first=150, last=179, thickness=24)
        self.assertEqual(defects.probe_video(take)[2], 240)  # 帧数不变
        # 相对源判定（MCP 同口径）：注入的下缘坏条可见，源固有的暗边不误报
        self.assertIn("bottom edge band f150-179", video_edge_bands(take, src=self.clip))
        self.assertEqual(video_edge_bands(self.clip, src=self.clip), [])  # 源相对自身无缺陷

    def test_cut_late_shifts_and_keeps_frames(self) -> None:
        take = self._take()
        defects.cut_shift(take, frames_late=12)
        self.assertEqual(defects.probe_video(take)[2], 240)
        self.assertEqual(defects.detect_cuts(take), [132])

    def test_cut_early_shifts_and_keeps_frames(self) -> None:
        take = self._take()
        defects.cut_shift(take, frames_early=12)
        self.assertEqual(defects.probe_video(take)[2], 240)
        self.assertEqual(defects.detect_cuts(take), [108])

    def test_cut_drop_freezes_through_cut(self) -> None:
        take = self._take()
        defects.cut_drop(take)
        self.assertEqual(defects.probe_video(take)[2], 240)
        self.assertEqual(defects.detect_cuts(take), [])  # 切点被吞

    def test_apply_defect_dispatch(self) -> None:
        take = self._take()
        defects.apply_defect(take, {"kind": "cut_late", "frames": 12})
        self.assertEqual(defects.detect_cuts(take), [132])
        with self.assertRaises(ValueError):
            defects.apply_defect(take, {"kind": "unknown"})

    def test_cut_shift_guard_rejects_out_of_range(self) -> None:
        take = self._take()
        with self.assertRaises(ValueError):
            defects.cut_shift(take, frames_early=200)  # k >= cut，无可移动范围
        with self.assertRaises(ValueError):
            defects.cut_shift(take, frames_early=12, frames_late=12)  # 须恰好一个为正

    def test_defect_plan_occurrence(self) -> None:
        plan = defects.DefectPlan(EDGE_PLAN)
        job = {"mode": "swap", "seed": 0, "tier": 480}
        self.assertEqual(plan.next_defect(job)["kind"], "edge_band")  # 第 1 个匹配 job 带缺陷
        self.assertIsNone(plan.next_defect(job))  # 第 2 个（修复后重渲）干净
        self.assertIsNone(plan.next_defect({"mode": "swap", "seed": 1, "tier": 480}))  # seed 1 不匹配 seed 0 规则


class SwapE2eHarness:
    """swap E2E 脚手架：home / 两镜头源 / 角色库 / 假 painter / 假 worker（可带缺陷计划）/ Hermes 配置。"""

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

    def start_worker(self, defect_plan: list[dict] | None = None) -> None:
        command = [sys.executable, str(PROJECT_ROOT / "scripts/fake_comfy_worker.py"),
                   "--home", str(self.home), "--beat-every", "2", "--poll", "1",
                   "--max-480", "345", "--max-736", "175"]
        if defect_plan is not None:
            plan_path = self.tmp / "defect_plan.json"
            plan_path.write_text(json.dumps(defect_plan), encoding="utf-8")
            command += ["--defect-plan", str(plan_path)]
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

    def run(self, entries: list[dict]) -> tuple[subprocess.CompletedProcess, Path, list[dict], list[dict]]:
        """fake_llm 回放驱动一次 swap 会话 → (进程结果, 会话目录, LLM 请求序列, painter 请求)。"""
        script = self.tmp / "script.jsonl"
        script.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries), encoding="utf-8")
        llm_log = self.tmp / "llm.jsonl"
        Handler.replay = Replay(script, "done")
        Handler.log_path = llm_log
        Handler.model_name = "fake-llm"
        self.llm = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.llm.serve_forever, daemon=True).start()
        llm_url = f"http://127.0.0.1:{self.llm.server_address[1]}/v1"
        (self.hermes_home / "config.yaml").write_text(self._mcp_config(llm_url, "fake-llm"), encoding="utf-8")
        prompt = edit_task_prompt(self.sid, str(self.src), kind="swap", dur=10.0, cast={"P1": "Vela"}, budget=8)
        completed, _workspace, session_dir = run_edit_session(
            PROJECT_ROOT, self.sid, prompt, home=self.home, base_url=llm_url, api_key="fake-key",
            extra_env={"PATH": self.path_env, "PYTHONUTF8": "1"}, timeout=600, verbose=True, log=lambda _m: None)
        time.sleep(0.2)
        requests = [json.loads(line)["request"] for line in llm_log.read_text(encoding="utf-8").splitlines()]
        painter_entries = ([json.loads(line) for line in self.painter_log.read_text(encoding="utf-8").splitlines()]
                           if self.painter_log.exists() else [])
        return completed, session_dir, requests, painter_entries

    def run_real(self, prompt: str) -> tuple[subprocess.CompletedProcess, Path]:
        """真 LLM 驱动（painter 仍假件、渲染零 GPU）：Hermes 指向 config.toml [llm] 三元组。"""
        if self.llm is not None:
            self.llm.shutdown()
            self.llm = None
        llm_url = LLM_CONFIG.llm_base_url
        (self.hermes_home / "config.yaml").write_text(self._mcp_config(llm_url, LLM_CONFIG.llm_model), encoding="utf-8")
        completed, _workspace, session_dir = run_edit_session(
            PROJECT_ROOT, self.sid, prompt, home=self.home, base_url=llm_url, api_key=LLM_CONFIG.llm_api_key,
            extra_env={"PATH": self.path_env, "PYTHONUTF8": "1"}, timeout=1200, verbose=True, log=lambda _m: None)
        return completed, session_dir

    def close(self) -> None:
        if self.worker is not None:
            self.worker.terminate()
        if self.llm is not None:
            self.llm.shutdown()
        self.painter.shutdown()


@unittest.skipUnless(E2E_READY, "未装 MCP venv/Hermes 运行时/ffmpeg（阶段 3/7）")
class SwapE2eTests(unittest.TestCase):
    """clean swap 全链路 + 三条审查-重试循环（零 GPU / 零 API 费）。"""

    def _assert_delivered(self, completed, session_dir, requests, entries, picks) -> None:
        self.assertEqual(completed.returncode, 0,
                         f"stderr: {completed.stderr[-1500:]}\nstdout: {completed.stdout[-1500:]}")
        self.assertEqual(executed_names(requests), [e["tool_calls"][0]["name"] for e in entries[:-1]])
        self.assertIn("Viggle Remake agent rules",
                      json.dumps(requests[0].get("messages", []), ensure_ascii=False))
        result = json.loads((session_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["picks"], picks)
        final = Path(result["final"])
        self.assertTrue(final.is_file(), result["final"])
        self.assertEqual(final.name, "full_480.mp4")

    def test_clean_swap_session(self) -> None:
        sid = "m43-clean"
        with tempfile.TemporaryDirectory(prefix="m43-clean-", ignore_cleanup_errors=True) as temporary:
            harness = SwapE2eHarness(Path(temporary), sid)
            harness.start_worker()
            try:
                entries = base_entries(sid, str(harness.src)) + [
                    tool("finish", {"sid": sid, "picks": {"0": 0}, "canvas": "480"}),
                    tool("save_result", {"sid": sid, "result": result_dict({"0": 0}, 1)}),
                    {"content": "delivered"}]
                completed, session_dir, requests, painter_entries = harness.run(entries)
            finally:
                harness.close()
            self._assert_delivered(completed, session_dir, requests, entries, {"0": 0})
            self.assertTrue(any((harness.home / "queue").glob("*.done")))
            self.assertTrue(any(e["endpoint"] == "edits" for e in painter_entries))  # paint 经假 painter
            for text in tool_results(requests, "strip"):  # 干净 take：strip 无缺陷量测
                self.assertNotIn("edge band", text)
                self.assertNotIn("cut timing", text)

    def test_edge_band_review_repaint_retry(self) -> None:
        sid = "m43-edge"
        with tempfile.TemporaryDirectory(prefix="m43-edge-", ignore_cleanup_errors=True) as temporary:
            harness = SwapE2eHarness(Path(temporary), sid)
            harness.start_worker(EDGE_PLAN)
            try:
                entries = base_entries(sid, str(harness.src)) + [
                    tool("paint", {"sid": sid, "frames": [180], "force": True, "quality": "low",
                                   "note": "the bottom edge band f150-179 comes from this keyframe carrying "
                                           "another frame's edge; repaint it clean"}),
                    tool("windows", {"sid": sid, "canvas": "480"}),
                    tool("write_prompts", {"sid": sid, "windows": [0]}),
                    tool("set_prompt", {"sid": sid, "window": 0, "text": SWAP_PROMPT}),
                    tool("submit", {"sid": sid, "seeds": [0], "canvas": "480"}),
                    tool("wait", {"sid": sid, "max_s": 120}),
                    tool("strip", {"sid": sid, "canvas": "480"}),
                    tool("finish", {"sid": sid, "picks": {"0": 0}, "canvas": "480"}),
                    tool("save_result", {"sid": sid, "result": result_dict({"0": 0}, 2)}),
                    {"content": "delivered"}]
                completed, session_dir, requests, painter_entries = harness.run(entries)
            finally:
                harness.close()
            self._assert_delivered(completed, session_dir, requests, entries, {"0": 0})
            strips = tool_results(requests, "strip")
            self.assertIn("bottom edge band f150-179", strips[0])  # 首 take 缺陷被 strip 量测到
            self.assertNotIn("edge band", strips[-1])  # 修复后重渲干净
            self.assertTrue(any(a.get("force") and a["frames"] == [180] for a in tool_args(requests, "paint")))
            self.assertGreaterEqual(sum(e["endpoint"] == "edits" for e in painter_entries), 2)  # 首绘 + 重绘

    def test_late_cut_review_seed1_retry(self) -> None:
        sid = "m43-late"
        with tempfile.TemporaryDirectory(prefix="m43-late-", ignore_cleanup_errors=True) as temporary:
            harness = SwapE2eHarness(Path(temporary), sid)
            harness.start_worker(LATE_PLAN)
            try:
                entries = base_entries(sid, str(harness.src)) + [
                    tool("submit", {"sid": sid, "seeds": [1], "windows": [0], "canvas": "480"}),
                    tool("wait", {"sid": sid, "max_s": 120}),
                    tool("strip", {"sid": sid, "canvas": "480"}),
                    tool("finish", {"sid": sid, "picks": {"0": 1}, "canvas": "480"}),
                    tool("save_result", {"sid": sid, "result": result_dict({"0": 1}, 1)}),
                    {"content": "delivered"}]
                completed, session_dir, requests, _painter = harness.run(entries)
            finally:
                harness.close()
            self._assert_delivered(completed, session_dir, requests, entries, {"0": 1})
            self.assertIn("cut f120 at f132 (+12)", tool_results(requests, "strip")[0])  # 切点延后被量测
            seeds = [a["seeds"] for a in tool_args(requests, "submit")]
            self.assertIn([0], seeds)
            self.assertIn([1], seeds)  # 修复 = seed 1 再渲，选切点落位的 take

    def test_early_cut_review_pin_retry(self) -> None:
        sid = "m43-early"
        with tempfile.TemporaryDirectory(prefix="m43-early-", ignore_cleanup_errors=True) as temporary:
            harness = SwapE2eHarness(Path(temporary), sid)
            harness.start_worker(EARLY_PLAN)
            try:
                entries = base_entries(sid, str(harness.src)) + [
                    tool("pin_before_cuts", {"sid": sid, "window": 0, "cuts": [120], "quality": "low"}),
                    tool("write_prompts", {"sid": sid, "windows": [0]}),
                    tool("set_prompt", {"sid": sid, "window": 0, "text": PINNED_PROMPT}),
                    tool("submit", {"sid": sid, "seeds": [0], "canvas": "480"}),
                    tool("wait", {"sid": sid, "max_s": 120}),
                    tool("strip", {"sid": sid, "canvas": "480"}),
                    tool("finish", {"sid": sid, "picks": {"0": 0}, "canvas": "480"}),
                    tool("save_result", {"sid": sid, "result": result_dict({"0": 0}, 2)}),
                    {"content": "delivered"}]
                completed, session_dir, requests, _painter = harness.run(entries)
            finally:
                harness.close()
            self._assert_delivered(completed, session_dir, requests, entries, {"0": 0})
            strips = tool_results(requests, "strip")
            self.assertIn("early on every seed: [120]", strips[0])  # 切点提前被量测且判定为输入问题
            self.assertNotIn("cut timing", strips[-1])  # pin 后重渲切点落位
            pins = tool_args(requests, "pin_before_cuts")
            self.assertEqual(pins[0]["cuts"], [120])  # 修复 = pin_before_cuts（换 seed 无效）
            self.assertEqual(pins[0]["window"], 0)


@unittest.skipUnless(REAL_LLM_READY,
                     "真 LLM 回归未开启：设 VIGGLE_REAL_LLM=1 且 config.toml [llm].api_key 非空"
                     "（真实 LLM token 费；painter 假件零图像费、渲染零 GPU）")
class RealLlmReviewTests(unittest.TestCase):
    """真 qwen3.7-plus 自主审查-修复：fake 预建边缘坏条缺陷会话 → 真 LLM 审查并触发重绘修复 → 交付。"""

    def test_real_llm_triggers_edge_band_repaint(self) -> None:
        sid = "m43-real-llm"
        with tempfile.TemporaryDirectory(prefix="m43-real-", ignore_cleanup_errors=True) as temporary:
            harness = SwapE2eHarness(Path(temporary), sid)
            harness.start_worker(EDGE_PLAN)
            try:
                # 阶段一：fake 回放把会话建到首次 strip（首 take 带边缘坏条缺陷）
                completed, session_dir, requests, _ = harness.run(base_entries(sid, str(harness.src)))
                self.assertEqual(completed.returncode, 0, completed.stderr[-1500:])
                self.assertIn("bottom edge band f150-179", tool_results(requests, "strip")[0])
                # 阶段二：真 LLM 自主审查-修复（painter 仍假件、worker 仍带缺陷计划：重渲 occ2 干净）
                resume = (f"Session id: {sid}. Kind: swap. Source clip: {harness.src}. Span: [0, 10) (seconds). "
                          f"Cast: P1 -> Vela. Resume this session: call status, then strip window 0 at canvas 480 "
                          f"and review the take against the source. It has a measured defect; apply the correct fix "
                          f"from AGENTS.md (edge bands: repaint the keyframe carrying that edge with force=true, then "
                          f"seed 0 again), re-render, review, finish and save_result. Run unattended: decide, do, deliver.")
                completed2, session_dir2 = harness.run_real(resume)
            finally:
                harness.close()
            self.assertEqual(completed2.returncode, 0,
                             f"stderr: {completed2.stderr[-1500:]}\nstdout: {completed2.stdout[-1500:]}")
            result = json.loads((session_dir2 / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "done")
            self.assertTrue(Path(result["final"]).is_file(), result["final"])
            # 核心断言：真 LLM 审查后触发了重绘修复动作（force=true 把旧绘挪到 paint/g2/old/）
            old = session_dir2 / "case/paint/g2/old"
            self.assertTrue(any(old.glob("f*_v*.png")), f"未见重绘修复证据：{list(old.glob('*')) if old.exists() else old}")
            self.assertGreaterEqual(len(list((harness.home / "queue").glob("*.done"))), 2)  # 首 take + 修复重渲


if __name__ == "__main__":
    unittest.main()