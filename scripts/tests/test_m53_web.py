"""M5.3 后端扩展零费单测：artifacts 产物清单 + settings 读写（config.toml 外科手术）。

前端组件测试在 app/web/（Vitest mock API）；本文件只覆盖 5.3 为前端补齐的两组端点
与 scripts/config.py 的 budget.max_images 解析，全部 tmp 目录零费。
"""

from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agent.hermes import AgentError
from app.server import settings
from app.server.api import create_app
from scripts.config import ConfigError, load_config

SAMPLE_TOML = """# 顶部注释保留
[comfyui]
mode = "bundled"

[llm]
# LLM 段注释
base_url = "https://old.example/v1"
api_key = "sk-secret-llm"
model = "qwen3.7-plus"

[painter]
base_url = "https://api.minimax.cn/v1"
api_key = "sk-secret-painter"
model = "MiniMax-H3-Image"

[budget]
# max_images = 200
"""


class ConfigBudgetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, text: str):
        (self.root / "config.toml").write_text(text, encoding="utf-8")

    def test_default_when_missing(self):
        self.assertEqual(load_config(self.root).budget_max_images, 24)

    def test_reads_max_images(self):
        self._write("[budget]\nmax_images = 12\n")
        self.assertEqual(load_config(self.root).budget_max_images, 12)

    def test_rejects_bad_values(self):
        for bad in ("0", "true", '"24"', "1.5"):
            self._write(f"[budget]\nmax_images = {bad}\n")
            with self.assertRaises(ConfigError):
                load_config(self.root)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (self.root / "config.toml").write_text(SAMPLE_TOML, encoding="utf-8")

    def test_read_masks_keys(self):
        view = settings.read_settings(self.root)
        self.assertTrue(view["llm"]["api_key_set"])
        self.assertTrue(view["painter"]["api_key_set"])
        self.assertNotIn("api_key", view["llm"])
        self.assertEqual(view["budget_default"], 24)
        self.assertEqual(view["llm"]["model"], "qwen3.7-plus")

    def test_update_preserves_comments_and_sections(self):
        view = settings.update_settings(self.root, {"llm": {"model": "qwen3.7-plus-2026-05-26"}})
        self.assertEqual(view["llm"]["model"], "qwen3.7-plus-2026-05-26")
        text = (self.root / "config.toml").read_text(encoding="utf-8")
        self.assertIn("# 顶部注释保留", text)
        self.assertIn("# LLM 段注释", text)
        self.assertIn('[comfyui]', text)
        self.assertIn('api_key = "sk-secret-llm"', text)  # 未给出的键不动
        self.assertIn('model = "MiniMax-H3-Image"', text)

    def test_empty_api_key_keeps_existing(self):
        settings.update_settings(self.root, {"llm": {"api_key": "", "base_url": "https://new.example/v1"}})
        cfg = load_config(self.root)
        self.assertEqual(cfg.llm_api_key, "sk-secret-llm")
        self.assertEqual(cfg.llm_base_url, "https://new.example/v1")

    def test_new_api_key_written(self):
        settings.update_settings(self.root, {"painter": {"api_key": "sk-fresh"}})
        self.assertEqual(load_config(self.root).painter_api_key, "sk-fresh")

    def test_appends_missing_key_and_section(self):
        # budget.max_images 只有注释行 → 追加真键；worker 段不存在 → 新建段不受影响
        settings.update_settings(self.root, {"budget": {"max_images": 30}})
        cfg = load_config(self.root)
        self.assertEqual(cfg.budget_max_images, 30)
        text = (self.root / "config.toml").read_text(encoding="utf-8")
        self.assertIn("# max_images = 200", text)  # 注释行保留
        self.assertIn("max_images = 30", text)

    def test_creates_file_when_absent(self):
        (self.root / "config.toml").unlink()
        view = settings.update_settings(self.root, {"llm": {"model": "m"}, "budget": {"max_images": 8}})
        self.assertEqual(view["llm"]["model"], "m")
        self.assertEqual(view["budget_default"], 8)
        self.assertTrue((self.root / "config.toml").is_file())

    def test_rejects_unknown_section_and_key(self):
        with self.assertRaises(ConfigError):
            settings.update_settings(self.root, {"worker": {"gpu": 1}})
        with self.assertRaises(ConfigError):
            settings.update_settings(self.root, {"llm": {"timeout": 5}})

    def test_rejects_bad_budget(self):
        for bad in (0, True, "24"):
            with self.assertRaises(ConfigError):
                settings.update_settings(self.root, {"budget": {"max_images": bad}})

    def test_quotes_escaped(self):
        settings.update_settings(self.root, {"llm": {"base_url": 'https://x/"q"'}})
        self.assertEqual(load_config(self.root).llm_base_url, 'https://x/"q"')


class ApiExtTests(unittest.TestCase):
    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self._tmp = tempfile.TemporaryDirectory()
        self._stack.callback(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root, self.home = base / "root", base / "home"
        self.root.mkdir()
        (self.root / "config.toml").write_text(SAMPLE_TOML, encoding="utf-8")
        # 进上下文触发 lifespan（退出时 store.close，避免 Windows 下 tmp 清理撞开着的 tasks.db）
        self.client = self._stack.enter_context(TestClient(create_app(self.root, self.home)))

    def _create_task(self) -> str:
        resp = self.client.post("/api/tasks",
                                files={"video": ("s.mp4", b"fake-video", "video/mp4")},
                                data={"description": "测试", "mode": "edit"})
        self.assertEqual(resp.status_code, 201)
        return resp.json()["task_id"]

    def test_artifacts_empty_then_lists(self):
        tid = self._create_task()
        empty = self.client.get(f"/api/tasks/{tid}/artifacts").json()
        self.assertEqual(empty, {"keyframes": [], "takes": [], "strips": [], "finals": [], "windows": []})
        session = self.home / "sessions" / tid
        case = session / "case"
        for rel in ("paint/g1/f60.png", "paint/g2/f120.png", "paint/src/f0.png",
                    "takes/w0_480_s0_1.mp4", "look/strips_0.jpg", "review/480_w0.jpg",
                    "final/full_480.mp4"):
            path = case / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        (case / "refs.json").write_text(json.dumps({"windows": [
            {"own": [0, 120], "render": [0, 124], "refs": [60], "passthrough": False},
            {"own": [120, 240], "render": [120, 244], "refs": [], "passthrough": True}]}),
            encoding="utf-8")
        (session / "result.json").write_text(json.dumps({
            "status": "done", "picks": {"0": 0},
            "windows": [{"w": 0, "grade": "OK", "note": "clean"}]}), encoding="utf-8")
        body = self.client.get(f"/api/tasks/{tid}/artifacts").json()
        self.assertEqual(body["keyframes"], ["case/paint/g1/f60.png", "case/paint/g2/f120.png"])
        self.assertEqual(body["takes"], ["case/takes/w0_480_s0_1.mp4"])
        self.assertEqual(body["strips"], ["case/review/480_w0.jpg"])  # 读片条带不列入审查对比条
        self.assertEqual(body["finals"], ["case/final/full_480.mp4"])
        self.assertEqual(body["windows"], [
            {"index": 0, "own": [0, 120], "render": [0, 124], "own_s": [0.0, 5.0],
             "passthrough": False, "picked_seed": 0, "grade": "OK", "note": "clean"},
            {"index": 1, "own": [120, 240], "render": [120, 244], "own_s": [5.0, 10.0],
             "passthrough": True, "picked_seed": None, "grade": None, "note": None}])

    def test_artifacts_windows_picks_fallback_and_broken_refs(self):
        tid = self._create_task()
        case = self.home / "sessions" / tid / "case"
        case.mkdir(parents=True, exist_ok=True)
        (case / "refs.json").write_text(json.dumps({"windows": [{"own": [0, 90], "render": [0, 90]}]}),
                                        encoding="utf-8")
        (case / "picks_480.json").write_text(json.dumps({"0": 1}), encoding="utf-8")  # result.json 缺 picks
        url = f"/api/tasks/{tid}/artifacts"
        window = self.client.get(url).json()["windows"][0]
        self.assertEqual((window["own_s"], window["picked_seed"]), ([0.0, 3.75], 1))
        (case / "refs.json").write_text("not json", encoding="utf-8")
        self.assertEqual(self.client.get(url).json()["windows"], [])  # 坏文件降级，不拖垮产物清单

    def test_artifacts_404(self):
        self.assertEqual(self.client.get("/api/tasks/nope/artifacts").status_code, 404)

    def test_settings_get_masks(self):
        body = self.client.get("/api/settings").json()
        self.assertTrue(body["llm"]["api_key_set"])
        self.assertNotIn("sk-secret", str(body))

    def test_settings_put_roundtrip(self):
        resp = self.client.put("/api/settings", json={
            "llm": {"model": "m2", "api_key": ""}, "budget": {"max_images": 16}})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["llm"]["model"], "m2")
        self.assertEqual(resp.json()["budget_default"], 16)
        cfg = load_config(self.root)
        self.assertEqual(cfg.llm_api_key, "sk-secret-llm")  # 空密钥保留现场
        self.assertEqual(cfg.budget_max_images, 16)

    def test_settings_put_bad_request(self):
        resp = self.client.put("/api/settings", json={"budget": {"max_images": 0}})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.put("/api/settings", json={"nope": {}})
        self.assertEqual(resp.status_code, 400)

    def test_settings_put_syncs_hermes_registration(self):
        """保存后重注册 Hermes：painter 密钥同步进 config.yaml/.env，下一任务即生效。"""
        with patch("app.server.api.write_hermes_config") as register:
            resp = self.client.put("/api/settings", json={"llm": {"model": "m2"}})
        self.assertEqual(resp.status_code, 200, resp.text)
        register.assert_called_once_with(self.root)

    def test_settings_put_survives_uninstalled_hermes(self):
        """Hermes 未安装（config.yaml 缺失）保存不报错，启动时自动补注册。"""
        with patch("app.server.api.write_hermes_config", side_effect=AgentError("未找到 Hermes config.yaml")):
            resp = self.client.put("/api/settings", json={"llm": {"model": "m2"}})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_create_task_requires_api_keys(self):
        """开箱无密钥：创建被拦并引导去设置页填写（服务本身正常启动）。"""
        (self.root / "config.toml").write_text('[llm]\napi_key = ""\n', encoding="utf-8")
        resp = self.client.post("/api/tasks",
                                files={"video": ("s.mp4", b"fake-video", "video/mp4")},
                                data={"description": "测试", "mode": "edit"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("设置", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()