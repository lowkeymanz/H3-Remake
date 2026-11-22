from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.config import Config, ConfigError, load_config, resolve_remake_home


class LoadConfigTests(unittest.TestCase):
    def test_defaults_when_config_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(load_config(Path(temporary)), Config())

    def test_parses_comfyui_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.toml").write_text(
                '[comfyui]\nmode = "external"\npath = "D:/ComfyUI"\n\n'
                '[paths]\nremake_home = "D:/data/remake"\n',
                encoding="utf-8",
            )
            config = load_config(root)
            self.assertEqual(config.comfyui_mode, "external")
            self.assertEqual(config.comfyui_path, "D:/ComfyUI")
            self.assertEqual(config.remake_home, "D:/data/remake")

    def test_invalid_values_raise_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"

            config.write_text('[comfyui]\nmode = "wrong"\n', encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "comfyui.mode"):
                load_config(root)

            config.write_text('[comfyui]\nmode = "external"\n', encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "comfyui.path"):
                load_config(root)

            config.write_text("[comfyui]\nmode = ", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "TOML"):
                load_config(root)


class WorkerConfigTests(unittest.TestCase):
    def test_worker_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = load_config(Path(temporary))
            self.assertEqual(config, Config())
            self.assertEqual(config.worker_gpu, 0)
            self.assertEqual(config.worker_max_480, 345)
            self.assertEqual(config.worker_max_736, 175)  # 需求定稿：--max-736 175
            self.assertEqual(config.worker_comfy_flags, ())

    def test_parses_worker_section(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.toml").write_text(
                '[worker]\ngpu = 1\nmax_480 = 300\nmax_736 = 175\n'
                'comfy_flags = ["--vram-headroom", "6"]\n',
                encoding="utf-8",
            )
            config = load_config(root)
            self.assertEqual(config.worker_gpu, 1)
            self.assertEqual(config.worker_max_480, 300)
            self.assertEqual(config.worker_max_736, 175)
            self.assertEqual(config.worker_comfy_flags, ("--vram-headroom", "6"))

    def test_worker_rejects_bad_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            for text, hint in (
                ("gpu = -1\n", "worker.gpu"),
                ("gpu = true\n", "worker.gpu"),
                ('max_480 = "345"\n', "worker.max_480"),
                ("max_736 = 175.5\n", "worker.max_736"),
                ("comfy_flags = [6]\n", "worker.comfy_flags"),
            ):
                config.write_text(f"[worker]\n{text}", encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, hint):
                    load_config(root)
            config.write_text("worker = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, r"\[worker\]"):
                load_config(root)


class ApiSectionConfigTests(unittest.TestCase):
    def test_llm_painter_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = load_config(Path(temporary))
            self.assertEqual(config.llm_model, "qwen3.7-plus")
            self.assertEqual(config.llm_base_url, "https://dashscope.aliyuncs.com/compatible-mode/v1")
            self.assertEqual(config.llm_api_key, "")
            self.assertEqual(config.painter_model, "MiniMax-H3-Image")
            self.assertEqual(config.painter_base_url, "https://api.minimax.cn/v1")
            self.assertEqual(config.painter_api_key, "")

    def test_parses_llm_painter_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.toml").write_text(
                '[llm]\nbase_url = "https://gw.example/v1"\napi_key = "sk-llm"\n'
                'model = "qwen3.7-plus-2026-05-26"\n\n'
                '[painter]\nbase_url = "https://paint.example/v1"\napi_key = "sk-paint"\n'
                'model = "MiniMax-H3-Image"\n',
                encoding="utf-8",
            )
            config = load_config(root)
            self.assertEqual(config.llm_model, "qwen3.7-plus-2026-05-26")
            self.assertEqual(config.llm_base_url, "https://gw.example/v1")
            self.assertEqual(config.llm_api_key, "sk-llm")
            self.assertEqual(config.painter_base_url, "https://paint.example/v1")
            self.assertEqual(config.painter_api_key, "sk-paint")

    def test_llm_painter_rejects_bad_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for text, hint in (
                ('[llm]\napi_key = 5\n', "llm.api_key"),
                ('[painter]\nmodel = ["x"]\n', "painter.model"),
                ("llm = 1\n", r"\[llm\]"),
            ):
                (root / "config.toml").write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, hint):
                    load_config(root)


class AgentConfigTests(unittest.TestCase):
    """[agent].round_timeout：真渲染会话小时级，缺省 7200s。"""

    def test_round_timeout_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(load_config(Path(temporary)).agent_round_timeout, 7200.0)

    def test_parses_round_timeout_int_and_float(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            config.write_text("[agent]\nround_timeout = 90\n", encoding="utf-8")
            self.assertEqual(load_config(root).agent_round_timeout, 90.0)
            config.write_text("[agent]\nround_timeout = 0.5\n", encoding="utf-8")
            self.assertEqual(load_config(root).agent_round_timeout, 0.5)

    def test_round_timeout_rejects_bad_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            for text, hint in (
                ("round_timeout = 0\n", "agent.round_timeout"),
                ("round_timeout = -1\n", "agent.round_timeout"),
                ("round_timeout = true\n", "agent.round_timeout"),
                ('round_timeout = "7200"\n', "agent.round_timeout"),
            ):
                config.write_text(f"[agent]\n{text}", encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, hint):
                    load_config(root)
            config.write_text("agent = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, r"\[agent\]"):
                load_config(root)


class ModelsConfigTests(unittest.TestCase):
    """[models].profile：档位字符串（注册表是档位合法性的唯一事实源，config 只校形状）。"""

    def test_profile_default_int8(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(load_config(Path(temporary)).models_profile, "int8")

    def test_parses_profile_string(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config.toml").write_text('[models]\nprofile = "gguf-q8"\n', encoding="utf-8")
            # 未注册档位不在 config 层拦截（由 scripts/models.py 注册表报错并列出可用档位）
            self.assertEqual(load_config(root).models_profile, "gguf-q8")

    def test_profile_rejects_bad_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            for text, hint in (
                ("profile = 8\n", "models.profile"),
                ('profile = ""\n', "models.profile"),
                ('profile = "  "\n', "models.profile"),
            ):
                config.write_text(f"[models]\n{text}", encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, hint):
                    load_config(root)
            config.write_text("models = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, r"\[models\]"):
                load_config(root)


class RemakeHomeResolutionTests(unittest.TestCase):
    def test_precedence_config_over_env_over_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # 默认：~/.viggle-remake
            with patch.dict(os.environ):
                os.environ.pop("REMAKE_HOME", None)
                self.assertEqual(resolve_remake_home(root), (Path.home() / ".viggle-remake").resolve())
            # 环境变量
            with patch.dict(os.environ, {"REMAKE_HOME": str(root / "from-env")}):
                self.assertEqual(resolve_remake_home(root), (root / "from-env").resolve())
            # config.toml 优先于环境变量
            (root / "config.toml").write_text(
                f'[paths]\nremake_home = "{(root / "from-config").as_posix()}"\n', encoding="utf-8")
            with patch.dict(os.environ, {"REMAKE_HOME": str(root / "from-env")}):
                self.assertEqual(resolve_remake_home(root), (root / "from-config").resolve())


if __name__ == "__main__":
    unittest.main()
