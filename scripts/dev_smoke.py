"""隔离状态下的 fake_worker 心跳与 MCP stdio 冒烟测试。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client


async def probe_mcp(mcp: Path, home: Path, env: dict[str, str]) -> int:
    parameters = StdioServerParameters(command=str(mcp), args=[], env=env)
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            if not {"new_session", "status"}.issubset(names):
                raise RuntimeError(f"MCP 缺少 new_session/status 工具；实际发现：{sorted(names)}")
            return len(names)


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_smoke(*, python: Path, mcp: Path, worker: Path) -> int:
    with tempfile.TemporaryDirectory(prefix="viggle-remake-test-") as temporary:
        home = Path(temporary)
        log_path = home / "fake_worker.log"
        env = {
            **os.environ,
            "REMAKE_HOME": str(home),
            "REMAKE_GUIDE_IN_AGENTS": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    str(python), str(worker), "--home", str(home), "--name", "install",
                    "--max480", "345", "--max736", "345", "--modes", "swap,edit", "--delay", "0",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
            )
            try:
                heartbeat = home / "workers/fake_install.json"
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"fake_worker 提前退出：{log_path.read_text(encoding='utf-8')}")
                    if heartbeat.is_file():
                        json.loads(heartbeat.read_text(encoding="utf-8"))
                        break
                    time.sleep(0.2)
                else:
                    detail = log_path.read_text(encoding="utf-8", errors="replace").strip()
                    raise RuntimeError(f"等待 fake_worker 心跳超时。日志：{detail or '<empty>'}")
                return asyncio.run(asyncio.wait_for(probe_mcp(mcp, home, env), timeout=45))
            finally:
                stop_process(process)
