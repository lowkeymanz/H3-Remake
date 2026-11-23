"""一键清理（云端部署测试后抹除个人痕迹；Ubuntu 优先设计，Windows 亦可运行）。

用法：
    uv run clean.py            # 预览将清理的内容，确认后执行
    uv run clean.py --dry-run  # 只预览不删除
    uv run clean.py --yes      # 跳过确认直接执行（无人值守）
    uv run clean.py --force    # 检测到在线 worker 心跳时仍强制执行

清理范围（四类，均为测试期间生成）：
  1. 任务产物与上传素材：$REMAKE_HOME 下 sessions/ uploads/ queue/ workers/
     agents/ chars/ logs/，tasks.db(+wal/shm/journal)、STOP 门闸、根级 *.jsonl 假件产物；
  2. Agent 会话痕迹：$REMAKE_HOME/hermes 下对话轨迹/记忆/请求转储/缓存/遥测/
     网关与运行时状态（sessions/ logs/ memories/ runtime/ state/ platforms/
     gateway/ cron/ cache/ image_cache/ audio_cache/ telemetry/ pending_messages/
     backups/、state.db*、projects.db、kanban.db*、spawn-ledger.json 等）；
  3. 运行时残留：/tmp（或 $TMPDIR）中本项目的临时目录（MCP peek 提帧崩溃残留；
     云平台镜像保存是磁盘快照，/tmp 内容会一并固化）、vendor 内 ComfyUI 的
     input/output/temp 内容、项目树 __pycache__/.pytest_cache 等缓存；
  4. 敏感配置：config.toml 的 [llm]/[painter] api_key；hermes/.env、dev/.env 中
     未注释的 API_KEY/TOKEN/SECRET/PASSWORD 赋值行清空；hermes/config.yaml 的
     mcp_servers env 明文密钥清空（保留结构与路径）；~/.bash_history、
     ~/.zsh_history、~/.python_history 中含密钥特征的行删除。

刻意保留（属环境与配置骨架，非测试产物）：
  - 项目环境：.venv、vendor/ 本体（ComfyUI/Hermes/原项目代码）、models.py 下载的
    模型、uv/HuggingFace 缓存；
  - Hermes 安装落位件：config.yaml 结构与 bin/ tools/ skills/ plugins/ installs/
    （config.yaml 仅抹明文密钥，注册条目/路径保留）；
  - config.toml 其余配置（base_url/model/预算/档位/路径）原样保留，仅抹密钥。

执行前请先停止 start.py（后端 + worker 守护）。检测到 120s 内的 worker
心跳时默认拒绝执行，--force 可越过。systemd 持久日志与当前 shell 的内存
历史不在本脚本权限内，收尾时会打印对应的手动命令。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from scripts.config import resolve_remake_home

ROOT = Path(__file__).resolve().parent

# 清单条目类型：dir=整删 / dir_empty=整删后重建空目录 / file=删文件 / sanitize=改写
Item = tuple[str, Path, int, str]

# ── $REMAKE_HOME 根级清理目标 ──────────────────────────────────────────
HOME_DIRS = ("sessions", "uploads", "queue", "workers", "agents", "chars", "logs")
HOME_FILES = ("tasks.db", "tasks.db-wal", "tasks.db-shm", "tasks.db-journal", "STOP")
# 清理后重建的空目录（MCP/后端按目录存在使用；queue/workers 由 worker 首写创建）
HOME_DIRS_KEEP_EMPTY = HOME_DIRS

# ── hermes 数据目录内的会话痕迹（整目录清空，保留目录本身）─────────────
HERMES_WIPE_DIRS = ("sessions", "logs", "memories", "runtime", "state", "platforms",
                    "gateway", "cron", "image_cache", "audio_cache", "cache",
                    "telemetry", "pending_messages", "backups")
# 相对 hermes 根的文件（存在才处理）
HERMES_WIPE_FILES = ("state.db", "state.db-wal", "state.db-shm",
                     "projects.db", "projects.db-wal", "projects.db-shm",
                     "kanban.db", "kanban.db-wal", "kanban.db-shm",
                     "gateway-starts.log", "spawn-ledger.json",
                     "channel_directory.json", "gateway_state.json")

# ── /tmp 残留（MCP peek 提帧 / 本项目测试的临时目录）──────────────────
TMP_PREFIXES = ("viggle-remake-test-", "m24-bench-", "m31-probe-")
TMP_MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".mp4"}
# 通用 tmpXXXXXXXX 目录仅含媒体文件时才视为 peek 残留；Windows %TEMP% 与其他
# 程序共享，默认只按前缀清（冒烟测试可强制打开）
GENERIC_TMP_SCAN = os.name == "posix"

# ── vendor 内 ComfyUI 的运行目录（内容清空，目录保留）────────────────
COMFY_TREES = ("vendor/ComfyUI", "vendor/viggle-remake/mcp/comfy_worker/ComfyUI")
COMFY_IO_SUBDIRS = ("input", "output", "temp")

# ── 项目树内可清的缓存（.venv/vendor/node_modules 剪枝跳过）──────────
PYCACHE_PRUNE = {".git", ".venv", "vendor", "node_modules"}
ROOT_CACHES = (".pytest_cache", ".ruff_cache", ".mypy_cache")

# ── shell/REPL 历史文件 ────────────────────────────────────────────────
HISTORY_FILES = (".bash_history", ".zsh_history", ".python_history")

# .env 中视为敏感的配置名（未注释赋值行一律清空）
ENV_SECRET_RE = re.compile(
    r"^(?P<name>[A-Z][A-Z0-9_]*?(?:API_KEY|TOKEN|SECRET|PASSWORD))\s*=.*$")
# config.yaml 中视为明文的敏感赋值（OPENAI_API_KEY / api_key / *_TOKEN 等）
YAML_SECRET_RE = re.compile(
    r'^(?P<pre>\s*)(?P<name>[\w.-]*(?:api_key|API_KEY|TOKEN|SECRET|PASSWORD)[\w.-]*)'
    r'\s*:\s*(?P<val>\S.*)$')
# 历史/REPL 文件中视为密钥的行特征
HISTORY_SECRET_RE = re.compile(
    r"sk-[A-Za-z0-9_.\-]{16,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}"
    r"|Bearer\s+[A-Za-z0-9._\-]{20,}"
    r"|(?:API_KEY|TOKEN|SECRET|PASSWORD)[=: ]+[\"']?[^\s\"']{12,}")
# config.toml 中要抹密钥的段
KEY_SECTIONS = ("llm", "painter")


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _fmt(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _human_live(home: Path) -> bool:
    """workers/ 心跳 120s 内更新过 → 判定有在线 worker，拒绝清理。"""
    workers = home / "workers"
    if not workers.is_dir():
        return False
    fresh = time.time() - 120
    return any(p.stat().st_mtime > fresh for p in workers.glob("*.json"))


def _tmp_root() -> Path:
    return Path(tempfile.gettempdir())


def _tmp_targets() -> list[Path]:
    """/tmp 中可安全认定为本项目残留的目录。"""
    root = _tmp_root()
    if not root.is_dir():
        return []
    out: list[Path] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    for p in entries:
        try:
            if not p.is_dir():
                continue
            if p.name.startswith(TMP_PREFIXES):
                out.append(p)
            elif GENERIC_TMP_SCAN and re.fullmatch(r"tmp[A-Za-z0-9_]{8}", p.name):
                files = [f for f in p.rglob("*") if f.is_file()]
                if files and all(f.suffix.lower() in TMP_MEDIA_EXTS for f in files):
                    out.append(p)  # 只含图片/视频的 tmpXXXXXXXX → peek 崩溃残留
        except OSError:
            continue
    return out


def _comfy_io_targets() -> list[Path]:
    """vendor 内 ComfyUI 的 input/output/temp（渲染直写 sessions，此处为防御性清扫）。"""
    out: list[Path] = []
    for rel in COMFY_TREES:
        for sub in COMFY_IO_SUBDIRS:
            p = ROOT / rel / sub
            try:
                if p.is_dir() and any(p.iterdir()):
                    out.append(p)
            except OSError:
                continue
    return out


def _pycache_targets() -> list[Path]:
    """项目树 __pycache__ 与根级工具缓存（.venv/vendor/node_modules 剪枝跳过）。"""
    out: list[Path] = []
    for name in ROOT_CACHES:
        p = ROOT / name
        if p.is_dir():
            out.append(p)
    for dirpath, dirnames, _filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in PYCACHE_PRUNE]
        if os.path.basename(dirpath) == "__pycache__":
            out.append(Path(dirpath))
            dirnames[:] = []
    return out


def _history_paths() -> list[Path]:
    return [Path.home() / name for name in HISTORY_FILES]


def collect(home: Path) -> list[Item]:
    """返回 (说明, 路径, 字节数, 类型) 清单，仅含实际存在的目标。"""
    items: list[Item] = []
    for name in HOME_DIRS:
        p = home / name
        if p.is_dir():
            items.append((f"任务产物/上传素材：{name}/", p, _dir_size(p), "dir"))
    for name in HOME_FILES:
        p = home / name
        if p.is_file():
            items.append((f"任务状态/门闸：{name}", p, p.stat().st_size, "file"))
    for p in sorted(home.glob("*.jsonl")):  # fake_worker 假件产物
        if p.is_file():
            items.append((f"假件队列产物：{p.name}", p, p.stat().st_size, "file"))
    hermes = home / "hermes"
    if hermes.is_dir():
        for name in HERMES_WIPE_DIRS:
            p = hermes / name
            if p.is_dir():
                items.append((f"Agent 会话痕迹：hermes/{name}/", p, _dir_size(p), "dir_empty"))
        for rel in HERMES_WIPE_FILES:
            p = hermes / rel
            if p.is_file():
                items.append((f"Agent 会话状态：hermes/{rel}", p, p.stat().st_size, "file"))
    for p in _tmp_targets():
        items.append((f"临时目录残留（{_tmp_root()}）：{p.name}/", p, _dir_size(p), "dir"))
    for p in _comfy_io_targets():
        rel = p.relative_to(ROOT)
        items.append((f"ComfyUI 运行目录：{rel.as_posix()}/", p, _dir_size(p), "dir_empty"))
    for p in _pycache_targets():
        rel = p.relative_to(ROOT)
        items.append((f"Python 缓存：{rel.as_posix()}/", p, _dir_size(p), "dir"))
    cfg = ROOT / "config.toml"
    if cfg.is_file():
        items.append(("敏感配置：config.toml 的 llm/painter api_key", cfg, 0, "sanitize"))
    env = hermes / ".env"
    if env.is_file():
        items.append(("敏感配置：hermes/.env 密钥赋值行", env, 0, "sanitize"))
    yml = hermes / "config.yaml"
    if yml.is_file():
        items.append(("敏感配置：hermes/config.yaml mcp env 明文密钥", yml, 0, "sanitize"))
    dev_env = ROOT / "dev" / ".env"
    if dev_env.is_file():
        items.append(("敏感配置：dev/.env 密钥赋值行", dev_env, 0, "sanitize"))
    for hist in _history_paths():
        if hist.is_file():
            items.append((f"历史文件含密钥行：~/{hist.name}", hist, 0, "sanitize"))
    return items


def sanitize_config_toml(path: Path) -> bool:
    """[llm]/[painter] 段内 api_key 置空；保留注释与其余配置。"""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    section: str | None = None
    changed = False
    for i, line in enumerate(lines):
        head = re.match(r"^\s*\[([\w.-]+)\]\s*(#.*)?$", line)
        if head:
            section = head.group(1)
            continue
        if section in KEY_SECTIONS and re.match(r"^\s*api_key\s*=", line):
            if line.strip() != 'api_key = ""':
                lines[i] = 'api_key = ""\n'
                changed = True
    if changed:
        path.write_text("".join(lines), encoding="utf-8")
    return changed


def sanitize_env_file(path: Path) -> bool:
    """.env 中未注释的敏感赋值行清空（值置空，键保留）。"""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = False
    for i, line in enumerate(lines):
        m = ENV_SECRET_RE.match(line.rstrip("\n"))
        if m and (line.rstrip("\n").split("=", 1)[1].strip() if "=" in line else ""):
            lines[i] = f"{m.group('name')}=\n"
            changed = True
    if changed:
        path.write_text("".join(lines), encoding="utf-8")
    return changed


def sanitize_config_yaml(path: Path) -> bool:
    """config.yaml 未注释的敏感赋值置空（${VAR} 引用与注释行不动）。"""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        m = YAML_SECRET_RE.match(line.rstrip("\n"))
        if not m:
            continue
        val = m.group("val").strip()
        if val.startswith("${") or val in ('""', "''"):
            continue
        lines[i] = f'{m.group("pre")}{m.group("name")}: ""\n'
        changed = True
    if changed:
        path.write_text("".join(lines), encoding="utf-8")
    return changed


def sanitize_history(path: Path) -> int:
    """删除历史文件中含密钥特征的行，返回删除行数。"""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    kept = [line for line in lines if not HISTORY_SECRET_RE.search(line)]
    removed = len(lines) - len(kept)
    if removed:
        path.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一键清理测试产物与敏感配置")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不删除")
    parser.add_argument("--yes", action="store_true", help="跳过确认")
    parser.add_argument("--force", action="store_true", help="忽略在线 worker 心跳")
    args = parser.parse_args(argv)

    home = resolve_remake_home(ROOT)
    print(f"REMAKE_HOME = {home}")
    if not home.is_dir():
        print("目录不存在，无需清理。")
        return 0

    if not args.force and _human_live(home):
        print("检测到 120s 内的 worker 心跳，疑似服务仍在运行。"
              "请先停止 start.py 后再清理（或确认无误用 --force）。")
        return 1

    items = collect(home)
    if not items:
        print("未发现任何待清理内容。")
        return 0

    total = sum(size for _, _, size, _ in items)
    print(f"\n将清理 {len(items)} 项（合计约 {_fmt(total)}）：")
    for desc, path, size, kind in items:
        mark = "    改写" if kind == "sanitize" else f"{_fmt(size):>9}"
        print(f"  [{mark}]  {desc}\n              {path}")

    if args.dry_run:
        print("\n--dry-run：未做任何改动。")
        return 0

    if not args.yes:
        answer = input("\n确认清理？输入 yes 继续：").strip().lower()
        if answer != "yes":
            print("已取消。")
            return 0

    for desc, path, _, kind in items:
        try:
            if kind == "sanitize":
                if path.name == "config.toml":
                    sanitize_config_toml(path)
                elif path.name == "config.yaml":
                    sanitize_config_yaml(path)
                elif path.name in HISTORY_FILES:
                    sanitize_history(path)
                else:
                    sanitize_env_file(path)
            elif kind == "file":
                path.unlink(missing_ok=True)
            elif kind == "dir":
                shutil.rmtree(path)
            elif kind == "dir_empty":
                shutil.rmtree(path)
                path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"  失败：{desc} → {exc}", file=sys.stderr)
            return 1

    # $REMAKE_HOME 根级目录清后重建空壳
    for name in HOME_DIRS_KEEP_EMPTY:
        (home / name).mkdir(parents=True, exist_ok=True)

    print("\n清理完成。已抹除任务产物/日志/角色库/会话痕迹/临时残留与全部 API 密钥；"
          "环境与其余配置保留。下次使用前请在设置页重新填写 LLM / Painter 密钥。")
    if Path("/var/log/journal").is_dir():
        print("提示：检测到 systemd 持久日志（/var/log/journal）。若服务经 systemd 运行，"
              "其 stdout 日志不在本脚本权限内，请另行执行："
              "sudo journalctl --rotate --vacuum-time=1s")
    if any(p.is_file() for p in _history_paths()):
        print("提示：若当前打开的 shell 会话中曾粘贴过密钥，退出前请执行 history -c"
              "（否则退出时会回写历史文件）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
