"""Hermes 集成（M3.1）：向 viggle-remake-mcp 注册、生成 Hermes 运行配置、构造驱动命令。

口径来自 M1.4 实测（42 轮 LLM / 41 次工具调用的现场配置，2026-11-18）：

- Hermes 主目录 = $REMAKE_HOME/hermes（config.yaml 由官方安装器在阶段 7 落位，含大量注释与
  _config_version 迁移标记——本模块只做**外科手术式**键值替换，不整文件重写）；
- model 段三键：default（模型名）/ provider "custom" / base_url（国内 OpenAI 兼容网关）；
- mcp_servers."viggle-remake" 五键：command（MCP venv 可执行文件，正斜杠）/ args / timeout 4200 /
  env（REMAKE_HOME + painter 三元组 + REMAKE_GUIDE_IN_AGENTS=1）；
- .env 必须写入 OPENAI_API_KEY + OPENAI_BASE_URL **配对**（缺一则 Hermes 静默回落 openrouter）。

密钥安全：config.toml 中 api_key 为空时**保留** config.yaml / .env 里的既有密钥不覆盖
（防止抹掉用户已填好的现场配置）；非密钥键（base_url/model 等）始终以 config.toml 为准——
即使与现场值不同也归一为项目定版口径（含默认值）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Sequence

from scripts.config import Config, load_config, resolve_remake_home
from scripts.installer import InstallError, Installer, venv_python

MCP_SERVER_NAME = "viggle-remake"
MCP_TIMEOUT_SECONDS = 4200  # 单工具上限：渲染窗任务分钟级 + 排队等待
GUIDE_FLAG = "1"
MCP_DISCOVERY_TIMEOUT_SECONDS = 30  # 首次工具快照等待 MCP 发现的时限（见 set_discovery_timeout）


class AgentError(RuntimeError):
    """可向用户直接展示的 agent 层错误。"""


def _installer(root: Path, platform: str | None = None) -> Installer:
    try:
        return Installer(root, platform=platform)
    except InstallError as exc:
        raise AgentError(str(exc)) from exc


def mcp_executable(root: Path, *, platform: str | None = None) -> Path:
    """viggle-remake-mcp 可执行文件（阶段 3 装入 vendor/viggle-remake/.venv）。"""
    installer = _installer(root, platform)
    name = "viggle-remake-mcp.exe" if installer.platform == "win32" else "viggle-remake-mcp"
    executable = venv_python(installer.mcp_venv, installer.platform).parent / name
    if not executable.is_file():
        raise AgentError(f"未找到 {executable}；请先执行阶段 3（MCP 独立环境安装）。")
    return executable


def mcp_server_env(root: Path, config: Config | None = None, *,
                   home: Path | None = None) -> dict[str, str]:
    """MCP 子进程环境：REMAKE_HOME + painter 三元组（images API）+ AGENTS 引导开关。"""
    config = config or load_config(root)
    return {
        "REMAKE_HOME": (home or resolve_remake_home(root)).as_posix(),
        "OPENAI_BASE_URL": config.painter_base_url,
        "OPENAI_API_KEY": config.painter_api_key,
        "REMAKE_IMAGE_MODEL": config.painter_model,
        "REMAKE_GUIDE_IN_AGENTS": GUIDE_FLAG,
    }


def mcp_server_entry(root: Path, *, config: Config | None = None,
                     platform: str | None = None, home: Path | None = None) -> dict:
    """Hermes config.yaml `mcp_servers` 注册条目（M1.4 现场 schema）。"""
    config = config or load_config(root)
    return {
        "command": mcp_executable(root, platform=platform).as_posix(),
        "args": [],
        "timeout": MCP_TIMEOUT_SECONDS,
        "env": mcp_server_env(root, config, home=home),
    }


# ── config.yaml 外科手术 ─────────────────────────────────────────────────────


def _top_block(lines: list[str], header: str) -> tuple[int, int] | None:
    """顶层段 `header:` 的行号区间 [start, end)；无则 None。"""
    marker = f"{header}:"
    for index, line in enumerate(lines):
        if line.rstrip("\n") == marker:
            end = len(lines)
            for later in range(index + 1, len(lines)):
                if lines[later].strip() and not lines[later][0].isspace():
                    end = later
                    break
            return index, end
    return None


def _set_scalar(block_lines: list[str], key: str, value: str | None) -> list[str]:
    """段内两缩进键 `  key: "v"` 的替换/追加；value None 时保留原值不动。"""
    if value is None:
        return block_lines
    pattern = re.compile(rf"^  {re.escape(key)}:\s*")
    out: list[str] = []
    replaced = False
    for line in block_lines:
        if pattern.match(line):
            out.append(f'  {key}: "{value}"\n')
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f'  {key}: "{value}"\n')
    return out


def set_model_fields(text: str, *, model: str | None, base_url: str | None) -> str:
    """替换 config.yaml `model:` 段的 default / provider / base_url（M1.4 三键）。"""
    lines = text.splitlines(keepends=True)
    block = _top_block(lines, "model")
    if block is None:
        raise AgentError("Hermes config.yaml 中未找到顶层 model: 段。")
    start, end = block
    body = lines[start + 1:end]
    body = _set_scalar(body, "default", model)
    body = _set_scalar(body, "provider", "custom")
    body = _set_scalar(body, "base_url", base_url)
    return "".join(lines[:start + 1] + body + lines[end:])


def _entry_lines(entry: dict) -> list[str]:
    env = entry["env"]
    lines = [
        f"  {MCP_SERVER_NAME}:\n",
        f'    command: "{entry["command"]}"\n',
        "    args: []\n",
        f"    timeout: {entry['timeout']}\n",
        "    env:\n",
    ]
    lines += [f'      {key}: "{value}"\n' for key, value in env.items()]
    return lines


def _existing_env(entry_block: list[str]) -> dict[str, str]:
    """从既有 `  viggle-remake:` 块解析 env 键值（供空值时保留现场密钥）。"""
    found: dict[str, str] = {}
    pattern = re.compile(r'^      ([A-Z_]+): "?(.*?)"?\s*$')
    for line in entry_block:
        matched = pattern.match(line)
        if matched:
            found[matched.group(1)] = matched.group(2)
    return found


def set_discovery_timeout(text: str, seconds: float = MCP_DISCOVERY_TIMEOUT_SECONDS) -> str:
    """设置顶层 mcp_discovery_timeout / mcp_single_query_discovery_timeout（M3.3 修复）。

    Hermes 首次工具快照只等待后台 MCP 发现线程到该时限（config 默认 1.5s / 15s，
    ``hermes_cli/mcp_startup.py`` ``_resolve_discovery_timeout``）；本项目 MCP 独立 venv
    冷启动握手实测 8-10s，时限不够时首轮请求的 tools 不含 MCP 工具——LLM 一调 MCP
    工具即 "Unknown tool"，3 次后 hermes 放弃（oneshot 无轮间恢复机会）。上调后
    ``join`` 在发现完成瞬间返回，只增加真实握手时长的首响应延迟。
    """
    lines = text.splitlines(keepends=True)
    for key in ("mcp_discovery_timeout", "mcp_single_query_discovery_timeout"):
        pattern = re.compile(rf"^{re.escape(key)}:")
        out: list[str] = []
        replaced = False
        for line in lines:
            if pattern.match(line):
                out.append(f"{key}: {seconds}\n")
                replaced = True
            else:
                out.append(line)
        if not replaced:
            if out and not out[-1].endswith("\n"):
                out[-1] += "\n"
            out.append(f"{key}: {seconds}\n")
        lines = out
    return "".join(lines)


def set_tool_search_enabled(text: str, enabled: str = "off") -> str:
    """设置 `tools.tool_search.enabled`（M3.3 修复，默认 "off" 直出全部工具）。

    Hermes 默认 "auto"（≡"on"）：MCP 工具一律被延迟到 tool_search/tool_describe/
    tool_call 桥后面（``tools/tool_search.py`` ``is_deferrable_tool_name`` 对
    mcp-* 工具集无条件判可延迟），LLM 面前的 tools 列表与调用名均为
    `mcp__viggle_remake__<tool>` 命名空间名。本项目单 MCP server 27 个工具
    （~10K tokens），qwen3.7-plus 上下文 1M，直出无压力且让 fake 回放与真
    LLM 路径同构、消除桥接间接层——关掉 deferral，工具直接进 tools 列表。
    """
    lines = text.splitlines(keepends=True)
    block = _top_block(lines, "tools")
    if block is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + f'tools:\n  tool_search:\n    enabled: "{enabled}"\n'
    start, end = block
    body = lines[start + 1:end]
    ts_at = ts_end = None
    for index, line in enumerate(body):
        if line.rstrip("\n") == "  tool_search:":
            ts_at = index
            ts_end = len(body)
            for later in range(index + 1, len(body)):
                if body[later].strip() and not body[later].startswith("    "):
                    ts_end = later
                    break
            break
    if ts_at is None:
        body += ["  tool_search:\n", f'    enabled: "{enabled}"\n']
    else:
        segment = body[ts_at:ts_end]
        if not any(re.match(r"^    enabled:", line) for line in segment):
            segment.append(f'    enabled: "{enabled}"\n')
        else:
            segment = [re.sub(r"^    enabled: .*$", f'    enabled: "{enabled}"', line)
                       for line in segment]
        body = body[:ts_at] + segment + body[ts_end:]
    return "".join(lines[:start + 1] + body + lines[end:])


def upsert_mcp_server(text: str, entry: dict) -> str:
    """config.yaml `mcp_servers` 段中插入/替换 viggle-remake 条目（可重入，幂等）。"""
    lines = text.splitlines(keepends=True)
    fresh = dict(entry["env"])
    block = _top_block(lines, "mcp_servers")
    if block is None:
        if not text.endswith("\n"):
            text += "\n"
        return text + "\n" + "".join(["mcp_servers:\n", *_entry_lines(entry)])
    start, end = block
    body = lines[start + 1:end]

    entry_at = None
    for index, line in enumerate(body):
        if line.rstrip("\n") == f"  {MCP_SERVER_NAME}:":
            entry_at = index
            break
    if entry_at is None:
        return "".join(lines[:start + 1] + [_f for _f in body if _f] + _entry_lines(entry) + lines[end:])
    # 既有条目：到下一个同级键（`  xxx:`）为止整块替换；空值沿用现场
    stop = len(body)
    for later in range(entry_at + 1, len(body)):
        if re.match(r"^  \S", body[later]):
            stop = later
            break
    for key, value in _existing_env(body[entry_at:stop]).items():
        if not fresh.get(key):
            fresh[key] = value
    merged = {**entry, "env": fresh}
    return "".join(lines[:start + 1] + body[:entry_at] + _entry_lines(merged) + body[stop:] + lines[end:])


def _set_env_pair(lines: list[str], key: str, value: str | None) -> list[str]:
    """.env 中 `KEY=value` 行替换/追加；value None 时保留既有值（密钥不抹除）。"""
    if value is None:
        return lines
    pattern = re.compile(rf"^{re.escape(key)}=")
    out: list[str] = []
    replaced = False
    for line in lines:
        if pattern.match(line):
            out.append(f"{key}={value}\n")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}\n")
    return out


def write_hermes_config(
    root: Path,
    *,
    config: Config | None = None,
    platform: str | None = None,
    log: Callable[[str], None] = print,
) -> tuple[Path, Path]:
    """把 model 三键与 viggle-remake 注册条目写入 Hermes config.yaml，配对写入 .env。

    → (config.yaml, .env)。api_key 为空时保留既有密钥；两者均无则写空值（真跑前须填写）。
    """
    config = config or load_config(root)
    home = resolve_remake_home(root)
    hermes_home = home / "hermes"
    config_path = hermes_home / "config.yaml"
    if not config_path.is_file():
        raise AgentError(f"未找到 {config_path}；请先执行阶段 7（Hermes 运行时安装）后再注册。")

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentError(f"无法读取 {config_path}：{exc}") from exc

    text = set_model_fields(text, model=config.llm_model, base_url=config.llm_base_url)
    text = upsert_mcp_server(text, mcp_server_entry(root, config=config, platform=platform, home=home))
    text = set_discovery_timeout(text)
    text = set_tool_search_enabled(text)
    if not text.endswith("\n"):
        text += "\n"
    config_path.write_text(text, encoding="utf-8")

    env_path = hermes_home / ".env"
    try:
        env_lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        env_lines = []
    env_lines = _set_env_pair(env_lines, "OPENAI_API_KEY", config.llm_api_key or None)
    env_lines = _set_env_pair(env_lines, "OPENAI_BASE_URL", config.llm_base_url or None)
    if not env_lines or not env_lines[-1].endswith("\n"):
        env_lines[-1:] = [env_lines[-1].rstrip("\n") + "\n"] if env_lines else []
    env_path.write_text("".join(env_lines), encoding="utf-8")

    if not config.llm_api_key or not config.painter_api_key:
        log("[agent] config.toml 中 llm/painter api_key 为空：已保留既有值（若无则为空），"
            "真实调用前请在 config.toml 补齐。")
    log(f"[agent] Hermes 已注册 viggle-remake-mcp：{config_path}")
    return config_path, env_path


def hermes_python(root: Path, *, platform: str | None = None) -> Path:
    """Hermes 运行时解释器（阶段 7 官方安装器落位）。"""
    installer = _installer(root, platform)
    candidates = installer.hermes_runtime_candidates()
    if not candidates:
        raise AgentError(f"未找到 Hermes 运行时（{installer.hermes_home}）；请先执行阶段 7。")
    return candidates[0]


def hermes_command(
    root: Path,
    args: Sequence[str] = (),
    *,
    platform: str | None = None,
    home: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    """构造 `python -m hermes_cli.main <args...>` 驱动命令与所需环境（HERMES_HOME）。

    环境其余键沿用当前进程（.env 由 Hermes 自行加载）；args 为 Hermes CLI 参数
    （M3.3 起：-p 任务提示、--in 工作目录等，由调用方拼装）。home 覆盖
    REMAKE_HOME（M3.3 测试隔离：种子 config.yaml 与驱动进程须同 home）。
    """
    python = hermes_python(root, platform=platform)
    effective_home = home or resolve_remake_home(root)
    return (
        [str(python), "-m", "hermes_cli.main", *args],
        {"HERMES_HOME": str(effective_home / "hermes")},
    )
