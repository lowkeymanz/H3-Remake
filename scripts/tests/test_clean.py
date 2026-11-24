"""clean.py 脱敏函数单测：只清值、保结构、可重入，改写后文件仍是合法格式。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from clean import sanitize_config_toml, sanitize_config_yaml, sanitize_env_file, sanitize_history
from scripts.config import load_config

TOML_SAMPLE = (
    "# 用户注释\n"
    '[comfyui]\nmode = "bundled"\n\n'
    "[worker]\ngpu = 0\n\n"
    '[llm]\nbase_url = "https://gw/v1"\napi_key = "sk-llm-secret"\nmodel = "qwen3.7-plus"\n\n'
    '[painter]\nbase_url = "https://paint/v1"\napi_key = "sk-paint-secret"\n\n'
    '[other]\napi_key = "not-a-target"\n'
)


class SanitizeConfigTomlTests(unittest.TestCase):
    def test_blanks_only_llm_and_painter_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(TOML_SAMPLE, encoding="utf-8")
            self.assertTrue(sanitize_config_toml(path))
            text = path.read_text(encoding="utf-8")
            self.assertIn('api_key = ""', text)
            self.assertNotIn("sk-llm-secret", text)
            self.assertNotIn("sk-paint-secret", text)
            # 其余配置与注释原样保留
            self.assertIn("# 用户注释", text)
            self.assertIn('mode = "bundled"', text)
            self.assertIn('base_url = "https://gw/v1"', text)
            self.assertIn('model = "qwen3.7-plus"', text)
            self.assertIn('api_key = "not-a-target"', text)  # 非目标段不动

    def test_result_loads_as_empty_keys(self) -> None:
        """脱敏后文件必须仍可被 load_config 正常解析（证明没搞坏格式）。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config.toml"
            path.write_text(TOML_SAMPLE, encoding="utf-8")
            sanitize_config_toml(path)
            config = load_config(root)
            self.assertEqual(config.llm_api_key, "")
            self.assertEqual(config.painter_api_key, "")
            self.assertEqual(config.llm_base_url, "https://gw/v1")

    def test_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(TOML_SAMPLE, encoding="utf-8")
            sanitize_config_toml(path)
            once = path.read_text(encoding="utf-8")
            self.assertFalse(sanitize_config_toml(path))
            self.assertEqual(path.read_text(encoding="utf-8"), once)


class SanitizeEnvFileTests(unittest.TestCase):
    ENV_SAMPLE = (
        "# 注释行\n"
        "OPENAI_API_KEY=sk-live-key\n"
        "OPENAI_BASE_URL=https://gw/v1\n"
        "MY_TOKEN=tok-abc\n"
        "lower_key=keep\n"
        "#API_KEY=commented-secret\n"
    )

    def test_blanks_values_keeps_keys_comments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text(self.ENV_SAMPLE, encoding="utf-8")
            self.assertTrue(sanitize_env_file(path))
            text = path.read_text(encoding="utf-8")
            self.assertIn("OPENAI_API_KEY=\n", text)
            self.assertIn("MY_TOKEN=\n", text)
            self.assertNotIn("sk-live-key", text)
            self.assertNotIn("tok-abc", text)
            # 非敏感键、注释、小写键原样
            self.assertIn("OPENAI_BASE_URL=https://gw/v1", text)
            self.assertIn("# 注释行", text)
            self.assertIn("lower_key=keep", text)
            self.assertIn("#API_KEY=commented-secret", text)

    def test_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text(self.ENV_SAMPLE, encoding="utf-8")
            sanitize_env_file(path)
            once = path.read_text(encoding="utf-8")
            self.assertFalse(sanitize_env_file(path))
            self.assertEqual(path.read_text(encoding="utf-8"), once)


class SanitizeConfigYamlTests(unittest.TestCase):
    YAML_SAMPLE = (
        "mcp_servers:\n"
        '  viggle-remake:\n'
        "    command: /bin/mcp\n"
        "    timeout: 4200\n"
        "    env:\n"
        '      OPENAI_API_KEY: "sk-paint-live"\n'
        '      OPENAI_BASE_URL: "https://paint/v1"\n'
        '      REMAKE_HOME: "/root/.viggle-remake"\n'
        "    gateway_token: ${GATEWAY_TOKEN}\n"
        '    api_key: ""\n'
        "# api_key: commented-secret\n"
    )

    def test_blanks_plain_secret_keeps_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.yaml"
            path.write_text(self.YAML_SAMPLE, encoding="utf-8")
            self.assertTrue(sanitize_config_yaml(path))
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("sk-paint-live", text)
            self.assertIn('      OPENAI_API_KEY: ""\n', text)  # 缩进保留
            # 非敏感键、${VAR} 引用、已空值、注释行不动
            self.assertIn("command: /bin/mcp", text)
            self.assertIn("timeout: 4200", text)
            self.assertIn('OPENAI_BASE_URL: "https://paint/v1"', text)
            self.assertIn("gateway_token: ${GATEWAY_TOKEN}", text)
            self.assertIn('# api_key: commented-secret', text)

    def test_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.yaml"
            path.write_text(self.YAML_SAMPLE, encoding="utf-8")
            sanitize_config_yaml(path)
            once = path.read_text(encoding="utf-8")
            self.assertFalse(sanitize_config_yaml(path))
            self.assertEqual(path.read_text(encoding="utf-8"), once)


class SanitizeHistoryTests(unittest.TestCase):
    def test_removes_secret_lines_keeps_rest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".bash_history"
            path.write_text(
                "ls -la\n"
                'curl -H "Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz"\n'
                "export API_KEY='sk-1234567890abcdef1234'\n"
                "uv run start.py\n",
                encoding="utf-8",
            )
            removed = sanitize_history(path)
            self.assertEqual(removed, 2)
            self.assertEqual(path.read_text(encoding="utf-8"), "ls -la\nuv run start.py\n")

    def test_no_secret_no_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".bash_history"
            path.write_text("git status\nuv run clean.py --dry-run\n", encoding="utf-8")
            self.assertEqual(sanitize_history(path), 0)


if __name__ == "__main__":
    unittest.main()
