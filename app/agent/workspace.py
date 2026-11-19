"""AGENTS.md 规则注入（M3.2）：agent 会话工作目录的准备与规则文件落位。

复刻原项目 codex_exec.sh 的注入方式（其 22–32 行）：工作目录 AGENTS.md =
`data/AGENTS.md`（agent 规则：预算硬上限、流程顺序、查看每张图等）
+ 三段附加（write_prompts/cut_windows 在 REMAKE_GUIDE_IN_AGENTS=1 时不再重复的内容）：

1. `# Prompt-writing guide …` + `data/short_guide.md`（六段式 prompt 指南）；
2. swap 格式示例 `data/example_prompt.txt`；
3. edit 格式示例 `data/example_edit_prompt.txt`。

agent（Hermes，`--in` 指向本目录）启动时把 AGENTS.md 注入上下文；MCP 端开关已由
M3.1 的注册条目 env 固定传入（REMAKE_GUIDE_IN_AGENTS=1），规则随上下文压缩后重读生效。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from scripts.config import resolve_remake_home
from scripts.installer import InstallError, Installer

SID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

# 四件套：规则原文 + MCP 端在开关打开时省略的三段（拼接文案与 codex_exec.sh 逐字一致）
SOURCES = ("AGENTS.md", "short_guide.md", "example_prompt.txt", "example_edit_prompt.txt")
GUIDE_HEAD = ("\n\n# Prompt-writing guide (write_prompts and cut_windows do not repeat it)"
              "\n\n=== GUIDE ===\n")
SWAP_EXAMPLE_HEAD = ("\n\n=== FORMAT EXAMPLE, swap sessions (a different edit of a different clip "
                     "- copy its structure, tags and level of detail, not its content) ===\n")
EDIT_EXAMPLE_HEAD = "\n\n=== FORMAT EXAMPLE, edit sessions (same) ===\n"


class WorkspaceError(RuntimeError):
    """可向用户直接展示的工作目录错误。"""


def mcp_data_dir(root: Path) -> Path:
    """原项目 MCP 数据目录（阶段 2 落位）。"""
    try:
        source = Installer(root).mcp_source
    except InstallError as exc:
        raise WorkspaceError(str(exc)) from exc
    return source / "viggle_remake_mcp" / "data"


def build_agents_md(root: Path, *, data_dir: Path | None = None) -> str:
    """拼接工作目录 AGENTS.md（内容与原项目 codex_exec.sh 生成的逐字一致）。"""
    data = data_dir or mcp_data_dir(root)
    texts: dict[str, str] = {}
    missing = []
    for name in SOURCES:
        path = data / name
        if not path.is_file():
            missing.append(str(path))
            continue
        texts[name] = path.read_text(encoding="utf-8")
    if missing:
        raise WorkspaceError(f"AGENTS.md 注入源缺失：{'、'.join(missing)}；请先执行阶段 2（原项目源码下载）。")
    return "".join((
        texts["AGENTS.md"],
        GUIDE_HEAD,
        texts["short_guide.md"],
        SWAP_EXAMPLE_HEAD,
        texts["example_prompt.txt"],
        EDIT_EXAMPLE_HEAD,
        texts["example_edit_prompt.txt"],
    ))


def prepare_workspace(root: Path, sid: str, *, log: Callable[[str], None] = print) -> Path:
    """准备 agent 会话工作目录 $REMAKE_HOME/agents/<sid>/ 并写入 AGENTS.md（幂等）。

    → 工作目录路径；M3.3 的编排以 `--in <dir>` 交给 Hermes。
    """
    if not SID_PATTERN.match(sid):
        raise WorkspaceError(f"会话 id 只允许字母/数字/点/下划线/连字符：{sid!r}")
    home = resolve_remake_home(root)
    workspace = home / "agents" / sid
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / "AGENTS.md"
    content = build_agents_md(root)
    if not target.is_file() or target.read_text(encoding="utf-8") != content:
        target.write_text(content, encoding="utf-8")
        log(f"[agent] AGENTS.md 已注入工作目录：{target}")
    else:
        log(f"[agent] AGENTS.md 已在位，无需重写：{target}")
    return workspace
