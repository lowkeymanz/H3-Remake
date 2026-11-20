"""M3.1：app/agent——Hermes 注册 viggle-remake-mcp + config.yaml/.env 生成 + 工具发现与调用。

纯文本工具（new_session / status）零 API 费；真实 MCP 集成用例在未装 MCP venv 的环境自动跳过。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent import hermes
from app.agent.hermes import (
    AgentError,
    hermes_command,
    mcp_executable,
    mcp_server_entry,
    write_hermes_config,
)
from scripts.installer import Installer, venv_python

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REAL_MCP = venv_python(PROJECT_ROOT / "vendor/viggle-remake/.venv", "win32").parent \
    / "viggle-remake-mcp.exe"

SEEDED_CONFIG = """\
# Hermes Agent CLI Configuration
_config_version: 53

model:
  # Default model to use (can be overridden with --model flag)
  default: "placeholder"
  provider: "auto"
  # commented out: base_url: "https://ignored.example/v1"
  base_url: ""

# 另一个顶层段（应保持不动）
database:
  journal_mode: "wal"

mcp_servers:
  existing-server:
    command: "C:/somewhere/other.exe"
    args: ["--flag"]
    timeout: 60
"""


def make_agent_root(tmp: str | Path, *, llm: str = "", painter: str = "") -> Path:
    """含 MCP venv 假件与 config.toml 的项目根。"""
    root = Path(tmp)
    executable = venv_python(root / "vendor/viggle-remake/.venv", "win32").parent \
        / "viggle-remake-mcp.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"")
    (root / "config.toml").write_text(
        f'[paths]\nremake_home = "{(root / "home").as_posix()}"\n'
        f"\n[llm]\n{llm}\n[painter]\n{painter}\n",
        encoding="utf-8",
    )
    return root


def seed_hermes_config(root: Path, text: str = SEEDED_CONFIG) -> Path:
    hermes_home = root / "home" / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    config_path = hermes_home / "config.yaml"
    config_path.write_text(text, encoding="utf-8")
    return config_path


class McpExecutableTests(unittest.TestCase):
    def test_missing_mcp_venv_reports_stage3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.toml").write_text("[paths]\n", encoding="utf-8")
            with self.assertRaisesRegex(AgentError, "阶段 3"):
                mcp_executable(root, platform="win32")

    def test_resolves_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_agent_root(tmp)
            self.assertEqual(mcp_executable(root, platform="win32").name, "viggle-remake-mcp.exe")


class McpServerEntryTests(unittest.TestCase):
    def test_entry_matches_m14_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_agent_root(
                tmp,
                llm='base_url = "https://gw/v1"\napi_key = "sk-llm"\nmodel = "qwen3.7-plus"\n',
                painter='base_url = "https://paint/v1"\napi_key = "sk-paint"\nmodel = "MiniMax-H3-Image"\n',
            )
            entry = mcp_server_entry(root, platform="win32")
            self.assertTrue(entry["command"].endswith("viggle-remake-mcp.exe"))
            self.assertIn("/", entry["command"])  # 正斜杠（M1.4 现场口径）
            self.assertEqual(entry["args"], [])
            self.assertEqual(entry["timeout"], 4200)
            self.assertEqual(entry["env"], {
                "REMAKE_HOME": (root / "home").as_posix(),
                "OPENAI_BASE_URL": "https://paint/v1",
                "OPENAI_API_KEY": "sk-paint",
                "REMAKE_IMAGE_MODEL": "MiniMax-H3-Image",
                "REMAKE_GUIDE_IN_AGENTS": "1",
            })


class WriteHermesConfigTests(unittest.TestCase):
    def test_full_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_agent_root(
                tmp,
                llm='base_url = "https://gw/v1"\napi_key = "sk-llm"\nmodel = "qwen3.7-plus"\n',
                painter='base_url = "https://paint/v1"\napi_key = "sk-paint"\nmodel = "MiniMax-H3-Image"\n',
            )
            config_path = seed_hermes_config(root)
            logs: list[str] = []
            yaml_path, env_path = write_hermes_config(root, platform="win32", log=logs.append)

            self.assertEqual(yaml_path, config_path)
            text = config_path.read_text(encoding="utf-8")
            # model 三键（M1.4 现场口径）
            self.assertIn('default: "qwen3.7-plus"', text)
            self.assertIn('provider: "custom"', text)
            self.assertIn('base_url: "https://gw/v1"', text)
            self.assertNotIn('provider: "auto"', text)
            # 注册条目 + 既有服务保留
            self.assertIn("viggle-remake:", text)
            self.assertIn(f'command: "{mcp_executable(root, platform="win32").as_posix()}"', text)
            self.assertIn("timeout: 4200", text)
            self.assertIn('REMAKE_GUIDE_IN_AGENTS: "1"', text)
            self.assertIn("existing-server:", text)
            self.assertIn('command: "C:/somewhere/other.exe"', text)
            # 其余顶层段不动
            self.assertIn('journal_mode: "wal"', text)
            # .env 配对
            env_text = env_path.read_text(encoding="utf-8")
            self.assertIn("OPENAI_API_KEY=sk-llm\n", env_text)
            self.assertIn("OPENAI_BASE_URL=https://gw/v1\n", env_text)
            self.assertFalse(any("api_key 为空" in line for line in logs))

    def test_idempotent_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_agent_root(tmp)
            seed_hermes_config(root)
            write_hermes_config(root, platform="win32", log=lambda _m: None)
            first = (root / "home/hermes/config.yaml").read_text(encoding="utf-8")
            write_hermes_config(root, platform="win32", log=lambda _m: None)
            self.assertEqual(first, (root / "home/hermes/config.yaml").read_text(encoding="utf-8"))

    def test_empty_keys_preserve_existing_secrets(self) -> None:
        seeded = SEEDED_CONFIG.replace(
            'mcp_servers:\n',
            'mcp_servers:\n'
            '  viggle-remake:\n'
            '    command: "D:/old/mcp.exe"\n'
            '    args: []\n'
            '    timeout: 60\n'
            '    env:\n'
            '      REMAKE_HOME: "D:/old/home"\n'
            '      OPENAI_BASE_URL: "https://old/v1"\n'
            '      OPENAI_API_KEY: "sk-live"\n'
            '      REMAKE_IMAGE_MODEL: "Old-Model"\n'
            '      REMAKE_GUIDE_IN_AGENTS: "1"\n',
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = make_agent_root(tmp)  # config.toml 无 api_key（默认空）
            seed_hermes_config(root, seeded)
            (root / "home/hermes/.env").write_text(
                "OPENAI_API_KEY=sk-live-env\nOTHER=keep\n", encoding="utf-8")
            logs: list[str] = []
            write_hermes_config(root, platform="win32", log=logs.append)

            text = (root / "home/hermes/config.yaml").read_text(encoding="utf-8")
            self.assertIn('OPENAI_API_KEY: "sk-live"', text)  # 现场密钥不被抹掉
            self.assertIn('REMAKE_IMAGE_MODEL: "MiniMax-H3-Image"', text)  # 非密钥键归一到 config 口径
            self.assertIn('OPENAI_BASE_URL: "https://api.minimax.cn/v1"', text)
            self.assertIn('command: "', text)
            self.assertNotIn("D:/old/mcp.exe", text)  # command 本身按项目实际路径更新
            env_text = (root / "home/hermes/.env").read_text(encoding="utf-8")
            self.assertIn("OPENAI_API_KEY=sk-live-env\n", env_text)  # 既有 .env 键保留
            self.assertIn("OTHER=keep\n", env_text)
            self.assertIn("OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1\n",
                          env_text)  # base_url 非空按 config 写入
            self.assertTrue(any("api_key 为空" in line for line in logs))

    def test_missing_config_yaml_reports_stage7(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_agent_root(tmp)
            (root / "home/hermes").mkdir(parents=True)
            with self.assertRaisesRegex(AgentError, "阶段 7"):
                write_hermes_config(root, platform="win32", log=lambda _m: None)


class HermesCommandTests(unittest.TestCase):
    def test_missing_runtime_reports_stage7(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_agent_root(tmp)
            with self.assertRaisesRegex(AgentError, r"--stages 7"):
                hermes_command(root, platform="win32")

    def test_builds_driver_command_with_hermes_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_agent_root(tmp)
            runtime = root / "home/hermes/installs/x/environments/y/venv/Scripts/python.exe"
            runtime.parent.mkdir(parents=True)
            runtime.write_bytes(b"")
            (root / "home/hermes/runtime-python.txt").write_text(str(runtime), encoding="utf-8")
            # 空壳假解释器过不了启动校验；本用例只验命令拼装，健康校验另行覆盖
            with patch.object(Installer, "hermes_python_ok", return_value=True):
                command, env = hermes_command(root, ["-p", "hi"], platform="win32")
            self.assertEqual(command, [str(runtime), "-m", "hermes_cli.main", "-p", "hi"])
            self.assertEqual(env, {"HERMES_HOME": str(root / "home/hermes")})


class SetModelFieldsTests(unittest.TestCase):
    def test_inserts_keys_into_sparse_model_block(self) -> None:
        text = "model:\n  # nothing active here\n\nother: 1\n"
        result = hermes.set_model_fields(text, model="m1", base_url="https://b/v1")
        self.assertIn('default: "m1"', result)
        self.assertIn('provider: "custom"', result)
        self.assertIn('base_url: "https://b/v1"', result)
        self.assertIn("other: 1", result)

    def test_missing_model_block_raises(self) -> None:
        with self.assertRaisesRegex(AgentError, "model:"):
            hermes.set_model_fields("nope: 1\n", model="m", base_url="u")


class UpsertMcpServerTests(unittest.TestCase):
    def test_appends_section_when_missing(self) -> None:
        entry = {"command": "D:/x/mcp.exe", "args": [], "timeout": 4200,
                 "env": {"REMAKE_HOME": "D:/h", "REMAKE_GUIDE_IN_AGENTS": "1"}}
        text = hermes.upsert_mcp_server("model:\n  default: \"m\"\n", entry)
        self.assertIn("mcp_servers:\n  viggle-remake:\n", text)
        self.assertIn('command: "D:/x/mcp.exe"', text)
        again = hermes.upsert_mcp_server(text, entry)
        self.assertEqual(text, again)  # 幂等


@unittest.skipUnless(REAL_MCP.is_file(), "未安装 MCP venv（阶段 3）；跳过真实集成")
class RealMcpProbeTests(unittest.TestCase):
    """真实 viggle-remake-mcp：工具发现 + new_session/status 实调（隔离 REMAKE_HOME）。"""

    def test_discover_and_call_text_tools(self) -> None:
        from app.agent.probe import check_mcp_tools

        with tempfile.TemporaryDirectory(prefix="m31-probe-") as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            result = check_mcp_tools(PROJECT_ROOT, home=home, sid="m31-real", kind="edit")
            self.assertIn("new_session", result["tools"])
            self.assertIn("status", result["tools"])
            self.assertGreaterEqual(result["tool_count"], 27)  # 需求基线：27 工具
            self.assertIn("created (edit)", result["new_session"])
            self.assertIn("not ingested yet", result["status"])
            self.assertTrue((home / "sessions" / "m31-real" / "kind").is_file())


if __name__ == "__main__":
    unittest.main()
