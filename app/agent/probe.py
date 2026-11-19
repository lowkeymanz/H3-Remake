"""MCP 接入验证（M3.1）：工具发现 + 纯文本工具调用（new_session / status），零 API 费。

与阶段 8 的 scripts/dev_smoke.py（安装冒烟：MCP 启动 + 工具计数）互补：本模块属 agent 层，
验证的是 Hermes 即将使用的注册口径——按 mcp_server_entry 的环境拉起 viggle-remake-mcp，
initialize → tools/list（new_session/status 必须在列）→ tools/call 实调两项纯文本工具。
阶段 8 冒烟今后可直接复用本模块（届时 dev_smoke 移除重复代码）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.agent.hermes import mcp_executable, mcp_server_env
from scripts.config import resolve_remake_home

REQUIRED_TOOLS = ("new_session", "status")
PROBE_SID = "m31-probe"


def _tool_text(result) -> str:
    parts = [block.text for block in result.content if getattr(block, "text", None)]
    return "\n".join(parts)


async def _probe(executable: Path, env: dict[str, str], *, sid: str, kind: str) -> dict:
    params = StdioServerParameters(command=str(executable), args=[], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = [tool.name for tool in listed.tools]
            missing = [name for name in REQUIRED_TOOLS if name not in tools]
            if missing:
                raise AgentProbeError(f"工具发现缺失：{', '.join(missing)}；共发现 {len(tools)} 项。")

            created = await session.call_tool("new_session", {"sid": sid, "kind": kind})
            if created.is_error:
                raise AgentProbeError(f"new_session 调用失败：{_tool_text(created)}")
            status = await session.call_tool("status", {"sid": sid})
            if status.is_error:
                raise AgentProbeError(f"status 调用失败：{_tool_text(status)}")
            return {
                "tool_count": len(tools),
                "tools": tools,
                "new_session": _tool_text(created),
                "status": _tool_text(status),
            }


class AgentProbeError(RuntimeError):
    """工具发现或调用未达预期。"""


def check_mcp_tools(
    root: Path,
    *,
    home: Path | None = None,
    sid: str = PROBE_SID,
    kind: str = "edit",
) -> dict:
    """拉起 viggle-remake-mcp 并实调 new_session/status（隔离 home 可控副作用）。

    → {tool_count, tools, new_session, status}；new_session/status 未达预期时抛错。
    """
    executable = mcp_executable(root)
    effective_home = home or resolve_remake_home(root)
    env = mcp_server_env(root, home=effective_home)
    return asyncio.run(_probe(executable, env, sid=sid, kind=kind))
