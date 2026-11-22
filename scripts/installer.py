"""M2.1/M2.2：可重入的 Windows/Ubuntu 环境安装器（不含模型）。--dev 基础链路 + 阶段 4/6（ComfyUI/节点）；
阶段 1 含前端构建。模型下载为独立入口 models.py（阶段 5 编号保留、逻辑移至 scripts/models.py）。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Callable, Sequence

from scripts.config import Config, ConfigError, load_config, resolve_remake_home

HERMES_COMMIT = "ae2a81ccc00e9b7c06301523e5bb09608397149c"

COMFYUI_TAG = "v0.42.0"
COMFYUI_VERSION = "0.42.0"
COMFYUI_REPOSITORY = "https://github.com/comfyanonymous/ComfyUI.git"
TORCH_INDEX_URL = "https://download.pytorch.org/whl/cu130"
TORCH_PINS = ("torch==2.9.1", "torchvision==0.24.1", "torchaudio==2.9.1")

# ── remake_worker.py Windows 兼容补丁常量（成品双平台口径：win/linux 均支持真渲染）──
# resource 是 Unix-only 标准库模块（官方 worker 仅用于 .done 的 peak_rss_gib 诊断字段，MCP 不读）；
# win 上 rename 目标存在即抛 FileExistsError——心跳目标恒存在，不打补丁则心跳停更、worker 超 120s 被判死。
REMAKE_RESOURCE_IMPORT = "import resource  # noqa: E402"
REMAKE_PEAK_LINE = '"peak_rss_gib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 1),'
REMAKE_RESOURCE_BLOCK = """try:
    import resource  # noqa: E402
except ImportError:  # Windows: resource is Unix-only
    resource = None


def peak_rss_gib():
    # 峰值常驻内存（GiB）：Unix 走 getrusage（KiB），Windows 走 GetProcessMemoryInfo（字节）
    if resource is not None:
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 1)
    import ctypes
    from ctypes import wintypes

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.windll.psapi
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return 0.0
    return round(counters.PeakWorkingSetSize / 2**30, 1)"""


STAGE_NAMES = {
    "0": "阶段 0：前置自检",
    "1": "阶段 1：项目环境",
    "2": "阶段 2：原项目源码",
    "3": "阶段 3：MCP 独立环境",
    "4": "阶段 4：ComfyUI 准备",
    "6": "阶段 6：节点与 worker",
    "7": "阶段 7：Hermes 运行时",
    "8": "阶段 8：fake_worker / MCP 冒烟",
}
DEV_STAGES = ("0", "1", "2", "3", "7", "8")
FULL_STAGES = tuple(STAGE_NAMES)


class InstallError(RuntimeError):
    """可向用户直接展示的安装错误。"""


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.install-tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def venv_python(venv: Path, platform: str | None = None) -> Path:
    platform = platform or sys.platform
    return venv / ("Scripts/python.exe" if platform == "win32" else "bin/python")


def total_ram_gib() -> float:
    """宿主机物理内存总量（GiB）；无法探测时返回 0。"""
    if sys.platform == "win32":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return 0.0
        return status.ullTotalPhys / 2**30
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (AttributeError, OSError, ValueError):
        pass
    try:
        # 容器等受限环境 sysconf 可能不可用，回落 /proc/meminfo
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 2**20  # kB → GiB
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


class Installer:
    def __init__(
        self,
        root: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        platform: str | None = None,
        remake_home: Path | None = None,
        dry_run: bool = False,
    ) -> None:
        self.root = root.resolve()
        self.vendor = self.root / "vendor"
        self.source = self.vendor / "viggle-remake"
        self.mcp_source = self.source / "mcp"
        self.mcp_venv = self.source / ".venv"
        self.hermes_source = self.vendor / "hermes"
        try:
            self.config: Config = load_config(self.root)
        except ConfigError as exc:
            raise InstallError(f"config.toml 配置有误：{exc}") from exc
        self.remake_home = Path(remake_home or resolve_remake_home(self.root)).resolve()
        self.hermes_home = self.remake_home / "hermes"
        self.runner = runner or subprocess.run
        self.platform = platform or sys.platform
        self.dry_run = dry_run
        self.current_stage = "安装初始化"

    def log(self, message: str) -> None:
        print(f"[install] {message}", flush=True)

    def run_command(self, command: Sequence[str | Path], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        normalized = [str(part) for part in command]
        self.log(f"执行：{normalized[0]} {normalized[1] if len(normalized) > 1 else ''}")
        try:
            self.runner(normalized, cwd=cwd or self.root, env=env, check=True)
        except subprocess.CalledProcessError as exc:
            raise InstallError(f"命令退出码 {exc.returncode}：{' '.join(normalized)}") from exc
        except OSError as exc:
            raise InstallError(f"无法执行 {normalized[0]}：{exc}") from exc

    def run_dev(self) -> None:
        self._run_stages(DEV_STAGES, full_preflight=False)
        self.current_stage = "完成"
        self.log("开发环境安装完成；ComfyUI、节点与模型不属于 --dev 阶段。")

    def run_full(self) -> None:
        self._run_stages(FULL_STAGES, full_preflight=True)
        self.current_stage = "完成"
        self.log("环境安装完成；模型下载请运行 uv run models.py（独立入口），随后 uv run start.py 启动。")

    def run_stages(self, ids: Sequence[str]) -> None:
        if "5" in ids:
            raise InstallError("阶段 5（模型下载）已移至独立入口 models.py；请运行 uv run models.py。")
        unknown = [stage for stage in ids if stage not in STAGE_NAMES]
        if unknown:
            raise InstallError(f"未知阶段：{', '.join(unknown)}；可用阶段为 0–8（不含 5，模型下载见 models.py）。")
        ordered = sorted(dict.fromkeys(ids), key=int)
        self._run_stages(ordered, full_preflight="0" in ordered)
        self.current_stage = "完成"
        self.log(f"指定阶段执行完成：{'、'.join(ordered)}。")

    def _run_stages(self, ids: Sequence[str], *, full_preflight: bool) -> None:
        actions: dict[str, Callable[[], None]] = {
            "0": lambda: self.preflight(full=full_preflight),
            "1": self.prepare_project,
            "2": self.ensure_source,
            "3": self.install_mcp,
            "4": self.install_comfyui,
            "6": self.install_nodes_and_worker,
            "7": self.install_hermes,
            "8": self.smoke,
        }
        for stage in ids:
            self.current_stage = STAGE_NAMES[stage]
            self.log(f"=== {self.current_stage} ===")
            actions[stage]()

    def preflight(self, *, full: bool = False) -> None:
        missing = [name for name in ("uv", "git", "npm") if not shutil.which(name)]
        if missing:
            raise InstallError(f"缺少必要工具：{', '.join(missing)}；请先安装并加入 PATH 后重跑。")
        if full:
            if not shutil.which("nvidia-smi"):
                raise InstallError("未找到 nvidia-smi：完整安装需要 NVIDIA GPU 环境；本地开发请使用 --dev。")
            free_gib = shutil.disk_usage(self.root).free / 2**30
            if free_gib < 20:
                raise InstallError(f"磁盘空间不足：{self.root} 所在盘仅剩 {free_gib:.0f} GiB，环境安装需 ≥20 GiB"
                                   "（模型约 64 GiB 由 models.py 下载时另行检查）。")
        try:
            ffmpeg_dir = self.configure_ffmpeg()
            self.log(f"ffmpeg 已就绪：{ffmpeg_dir}")
        except InstallError:
            self.log("ffmpeg 尚未就绪；将在 Hermes 工具安装完成后再次检查。")

    def sync_project(self) -> None:
        self.run_command(["uv", "sync"])

    def prepare_project(self) -> None:
        """阶段 1 项目环境：Python 依赖（uv sync）+ 前端构建（app/web → dist）。"""
        self.sync_project()
        self.build_web()

    def missing_source_files(self, source: Path) -> list[str]:
        required = [
            "mcp/pyproject.toml",
            "mcp/viggle_remake_mcp/paint.py",
            "mcp/tests/fake_worker.py",
            "mcp/comfy_worker/remake_worker.py",
            "comfyui/ComfyUI-Viggle-Remake/__init__.py",
            "comfyui/ComfyUI-Viggle-Remake/nodes.py",
        ]
        return [name for name in required if not (source / name).is_file()]

    def validate_source(self, source: Path) -> None:
        absent = self.missing_source_files(source)
        if absent:
            raise InstallError(f"原项目源码不完整：{', '.join(absent)}")

    def download_source(self, destination: Path) -> None:
        self.run_command([
            "uvx", "--from", "huggingface_hub", "hf", "download", "Viggle/Viggle-Remake",
            "--include", "mcp/*", "--include", "comfyui/ComfyUI-Viggle-Remake/*",
            "--local-dir", destination,
        ])

    def ensure_source(self) -> None:
        if self.source.exists():
            absent = self.missing_source_files(self.source)
            if not absent:
                self.log("原项目源码已存在，跳过下载。")
                return
            self.log(f"原项目源码不完整（缺 {', '.join(absent)}），原地补齐。")
            self.download_source(self.source)
            self.validate_source(self.source)
            return

        self.vendor.mkdir(parents=True, exist_ok=True)
        staging = self.vendor / ".viggle-remake.download"
        staging.mkdir(parents=True, exist_ok=True)
        self.download_source(staging)
        self.validate_source(staging)
        staging.replace(self.source)
        self.validate_source(self.source)

    def apply_compatibility_patches(self) -> None:
        paint = self.mcp_source / "viggle_remake_mcp/paint.py"
        text = paint.read_text(encoding="utf-8")
        start = text.find("def gpt(")
        end = text.find("\ndef ", start + 1)
        if start < 0 or end < 0:
            raise InstallError("paint.py 中未找到 gpt()，无法应用 b64_json 兼容补丁。")
        block = text[start:end]
        if block.count('response_format="b64_json"') != 2:
            block = block.replace(
                "size=size, quality=quality)",
                'size=size, quality=quality, response_format="b64_json")',
            )
        if block.count('response_format="b64_json"') != 2:
            raise InstallError("paint.py 调用形式已变化；请人工核对 images.edit/generate 的 b64_json 补丁。")
        updated = text[:start] + block + text[end:]
        if updated != text:
            atomic_write(paint, updated)

        worker = self.mcp_source / "tests/fake_worker.py"
        text = worker.read_text(encoding="utf-8")
        updated = text.replace(
            'tmp.rename(workers / f"fake_{a.name}.json")',
            'tmp.replace(workers / f"fake_{a.name}.json")',
        ).replace('tmp.rename(job["out"])', 'tmp.replace(job["out"])')
        if 'tmp.replace(workers / f"fake_{a.name}.json")' not in updated or 'tmp.replace(job["out"])' not in updated:
            raise InstallError("fake_worker.py 写入形式已变化；请人工核对 Windows replace() 补丁。")
        if updated != text:
            atomic_write(worker, updated)

        self.patch_remake_worker()

    def patch_remake_worker(self) -> None:
        """remake_worker.py Windows 兼容补丁（幂等 + 形状校验；linux 侧零改动）。

        resource 条件导入（win 回落 ctypes 峰值内存）+ 心跳/.done/成片写入侧 rename→replace；
        认领/孤儿回队保留 rename（原子认领语义，与 fake_worker 补丁口径一致）。
        """
        worker = self.mcp_source / "comfy_worker/remake_worker.py"
        text = worker.read_text(encoding="utf-8")
        updated = text
        if "except ImportError:  # Windows: resource is Unix-only" not in updated:
            if updated.count(REMAKE_RESOURCE_IMPORT) != 1 or updated.count(REMAKE_PEAK_LINE) != 1:
                raise InstallError("remake_worker.py resource 形式已变化；请人工核对 Windows 兼容补丁。")
            updated = updated.replace(REMAKE_RESOURCE_IMPORT, REMAKE_RESOURCE_BLOCK, 1)
            updated = updated.replace(REMAKE_PEAK_LINE, '"peak_rss_gib": peak_rss_gib(),', 1)
        for old, new in (
            ("tmp.rename(beat_path)", "tmp.replace(beat_path)"),
            ("tmp.rename(dst)", "tmp.replace(dst)"),
            ('tmp.rename(run.with_suffix(".done"))', 'tmp.replace(run.with_suffix(".done"))'),
        ):
            if old in updated:
                updated = updated.replace(old, new)
            elif new not in updated:
                raise InstallError(f"remake_worker.py 写入形式已变化；请人工核对 Windows replace() 补丁：{old}")
        if updated != text:
            atomic_write(worker, updated)

    def install_mcp(self) -> None:
        self.apply_compatibility_patches()
        python = venv_python(self.mcp_venv, self.platform)
        if not python.is_file():
            self.run_command(["uv", "venv", "--python", "3.12", self.mcp_venv])
        if not python.is_file():
            raise InstallError(f"MCP venv 创建后仍缺少解释器：{python}")
        self.run_command(["uv", "pip", "install", "--python", python, self.mcp_source])

        executable = python.parent / ("viggle-remake-mcp.exe" if self.platform == "win32" else "viggle-remake-mcp")
        if not executable.is_file():
            raise InstallError(f"MCP 安装后缺少命令入口：{executable}")
        self.run_command([
            python,
            "-c",
            "import viggle_remake_mcp.server; import importlib.metadata as m; print('viggle-remake-mcp', m.version('viggle-remake-mcp'))",
        ])

    # ── 阶段 4：ComfyUI 准备（bundled / external / auto） ──

    def comfyui_version(self, comfy: Path) -> str | None:
        version_file = comfy / "comfyui_version.py"
        if not version_file.is_file():
            return None
        match = re.search(r'__version__\s*=\s*"([^"]+)"', version_file.read_text(encoding="utf-8"))
        return match.group(1) if match else None

    def comfyui_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        if self.config.comfyui_path:
            candidates.append(Path(self.config.comfyui_path).expanduser())
        candidates.append(Path.home() / "ComfyUI")
        if self.platform != "win32":
            candidates.append(Path("/opt/ComfyUI"))
        return candidates

    def resolve_comfyui(self) -> tuple[Path, str]:
        """解析 $COMFYUI 生效路径，返回 (路径, 来源)；后续环节（模型/节点/worker）只认这个结果。"""
        if self.config.comfyui_mode == "external":
            return self.resolve_external_comfyui()
        if self.config.comfyui_mode == "auto":
            for candidate in self.comfyui_candidates():
                if (candidate / "main.py").is_file() and self.comfyui_version(candidate) == COMFYUI_VERSION:
                    try:
                        answer = input(f"检测到已有 ComfyUI {COMFYUI_VERSION}：{candidate}\n按 external 模式使用它？[y/N] ")
                    except EOFError:
                        answer = ""
                    if answer.strip().lower() in ("y", "yes"):
                        self.log(f"auto 选定 external：{candidate}；建议写入 config.toml（mode = \"external\"、path）固化。")
                        return candidate.resolve(), "external"
                    break
            self.log("auto 模式未确认任何已有 ComfyUI，回落 bundled。")
        return self.vendor / "ComfyUI", "bundled"

    def resolve_external_comfyui(self) -> tuple[Path, str]:
        if not self.config.comfyui_path:
            raise InstallError('comfyui.mode = "external" 需要在 config.toml 配置 comfyui.path。')
        comfy = Path(self.config.comfyui_path).expanduser().resolve()
        if not (comfy / "main.py").is_file():
            raise InstallError(f"external ComfyUI 路径无效（缺少 main.py）：{comfy}")
        return comfy, "external"

    def install_comfyui(self) -> None:
        comfy, source = self.resolve_comfyui()
        version = self.comfyui_version(comfy)
        if source == "external":
            if version != COMFYUI_VERSION:
                raise InstallError(
                    f"external ComfyUI 版本为 {version or '未知'}，与实测兼容版本 {COMFYUI_VERSION} 不符；建议改用 bundled 模式。")
            python = self.external_comfyui_python(comfy)
            try:
                self.verify_comfyui_runtime(python)
            except InstallError as exc:
                raise InstallError(
                    f"external ComfyUI venv 校验失败（需 torch 2.9.x + torchvision 0.24.x）：{exc}；请修复 venv 或改用 bundled 模式。") from exc
            self.log(f"external ComfyUI 校验通过：{comfy}（venv：{python}）")
            return

        if not (comfy / "main.py").is_file():
            self.vendor.mkdir(parents=True, exist_ok=True)
            self.run_command(["git", "clone", "--depth", "1", "--branch", COMFYUI_TAG, COMFYUI_REPOSITORY, comfy])
            version = self.comfyui_version(comfy)
        if version != COMFYUI_VERSION:
            raise InstallError(f"ComfyUI 版本异常：期望 {COMFYUI_VERSION}，实际 {version or '未知'}（{comfy}）。")
        python = venv_python(comfy / "venv", self.platform)
        if not python.is_file():
            self.run_command(["uv", "venv", "--python", "3.12", comfy / "venv"])
        env = {**os.environ, "UV_HTTP_TIMEOUT": "600"}
        self.run_command(["uv", "pip", "install", "--python", python, *TORCH_PINS, "--index-url", TORCH_INDEX_URL], env=env)
        self.run_command(["uv", "pip", "install", "--python", python, "-r", self.filtered_requirements(comfy)], env=env)
        self.verify_comfyui_runtime(python)
        self.log(f"ComfyUI {COMFYUI_VERSION} 就绪（bundled）：{comfy}")

    def filtered_requirements(self, comfy: Path) -> Path:
        """生成剔除 comfyui-workflow-templates* 的 requirements 过滤副本（上游原文件不动）。

        该包仅供 ComfyUI Web UI 模板画廊（frontend_management 内 try/except ImportError 软依赖），
        worker 以库导入渲染不经过该路径；其锁定的 media-assets 依赖已从 PyPI 消失，
        原样安装必然解析失败，故过滤后安装。
        """
        original = comfy / "requirements.txt"
        lines = original.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = [line for line in lines if not line.strip().lower().startswith("comfyui-workflow-templates")]
        filtered = comfy / "requirements.viggle.txt"
        content = "".join(kept)
        if not filtered.is_file() or filtered.read_text(encoding="utf-8") != content:
            atomic_write(filtered, content)
        return filtered

    def external_comfyui_python(self, comfy: Path) -> Path:
        for name in ("venv", ".venv"):
            python = venv_python(comfy / name, self.platform)
            if python.is_file():
                return python
        raise InstallError(f"external ComfyUI 未找到可用 venv（尝试 {comfy}/venv 与 .venv）；请修复或改用 bundled 模式。")

    def verify_comfyui_runtime(self, python: Path) -> None:
        self.run_command([
            python, "-c",
            "import torch, torchvision; "
            "assert torch.__version__.startswith('2.9.'), torch.__version__; "
            "assert torchvision.__version__.startswith('0.24.'), torchvision.__version__",
        ])

    # ── 阶段 6：节点与 worker ──

    def install_nodes_and_worker(self) -> None:
        comfy, _source = self.resolve_comfyui()
        if not (comfy / "main.py").is_file():
            raise InstallError(f"ComfyUI 未就绪（{comfy}）；请先执行阶段 4。")
        self.install_node_package(comfy)
        self.link_worker_comfy(comfy)

    def install_node_package(self, comfy: Path) -> None:
        source = self.source / "comfyui" / "ComfyUI-Viggle-Remake"
        absent = [name for name in ("__init__.py", "nodes.py") if not (source / name).is_file()]
        if absent:
            raise InstallError(f"官方节点包源码缺失：{', '.join(absent)}（{source}）；请先执行阶段 2。")
        destination = comfy / "custom_nodes" / "ComfyUI-Viggle-Remake"
        self.ensure_dir_link(source.resolve(), destination, label="官方节点包")
        self.log(f"官方节点包已链接：{destination} → {source}")

    def link_worker_comfy(self, comfy: Path) -> None:
        worker_dir = self.mcp_source / "comfy_worker"
        if not worker_dir.is_dir():
            raise InstallError(f"comfy_worker 目录缺失：{worker_dir}；请先执行阶段 2。")
        link = worker_dir / "ComfyUI"
        self.ensure_dir_link(comfy.resolve(), link, label="comfy_worker 的 ComfyUI 入口")
        self.log(f"comfy_worker ComfyUI 链接就绪：{link} → {comfy}（remake_worker.py 零改动）")

    def _dir_link_target(self, link: Path) -> Path | None:
        """link 当前指向的绝对目标；不是链接（真实目录或不存在）时返回 None。"""
        parent = Path(os.path.realpath(link.parent)) if link.parent.exists() else Path(os.path.abspath(link.parent))
        resolved = Path(os.path.realpath(link))
        return resolved if resolved != parent / link.name else None

    def ensure_dir_link(self, target: Path, link: Path, *, label: str) -> None:
        target = target.resolve()
        current = self._dir_link_target(link)
        if current == target:
            return
        if link.exists() and current is None:
            raise InstallError(f"{label}：位置已被非链接内容占用，请人工处理：{link}")
        if current is not None:
            self.remove_dir_link(link)
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            if self.platform != "win32":
                raise InstallError(f"{label}：无法创建符号链接 {link} → {target}。") from None
            self.run_command(["cmd", "/c", "mklink", "/J", str(link), str(target)])
        if self._dir_link_target(link) != target:
            raise InstallError(f"{label}：链接创建后校验失败：{link} → {target}。")

    def remove_dir_link(self, link: Path) -> None:
        if self.platform == "win32":
            os.rmdir(link)  # 目录符号链接与 junction 均可如此移除
        else:
            link.unlink()

    def hermes_runtime_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        recorded = self.hermes_home / "runtime-python.txt"
        if recorded.is_file():
            candidates.append(Path(recorded.read_text(encoding="utf-8").strip()))
        runtime_glob = self.hermes_home.glob("installs/*/environments/*/venv")
        runtimes = sorted(
            (venv_python(path, self.platform) for path in runtime_glob),
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        candidates.extend(runtimes)
        return list(dict.fromkeys(path for path in candidates if path.is_file()))

    def verify_existing_hermes(self) -> Path | None:
        marker = self.hermes_source / ".hermes-bootstrap-complete"
        if not marker.is_file():
            return None
        try:
            pinned = json.loads(marker.read_text(encoding="utf-8-sig"))["pinnedCommit"]
        except (OSError, ValueError, KeyError) as exc:
            raise InstallError(f"Hermes 安装标记不可读：{marker}：{exc}") from exc
        if pinned != HERMES_COMMIT:
            raise InstallError(f"Hermes 已安装提交 {pinned} 与锁定提交 {HERMES_COMMIT} 不一致。")

        for python in self.hermes_runtime_candidates():
            if self._hermes_import_ok(python):
                atomic_write(self.hermes_home / "runtime-python.txt", f"{python}\n")
                return python
        return None

    def _hermes_import_ok(self, python: Path) -> bool:
        try:
            self.run_command(
                [python, "-c", "import hermes_cli.main; print('Hermes runtime OK')"],
                env={**os.environ, "HERMES_HOME": str(self.hermes_home)},
            )
            return True
        except InstallError:
            return False

    def hermes_python_ok(self, python: Path) -> bool:
        """启动级健康校验：解释器能进运行时即真（坏 venv 在启动即 Fatal error）。"""
        try:
            self.run_command([python, "-c", "pass"],
                             env={**os.environ, "HERMES_HOME": str(self.hermes_home)})
            return True
        except InstallError:
            return False

    def quarantine_broken_hermes_envs(self) -> list[Path]:
        """导入复验失败的 venv 挪边，官方安装器才会重建而非沿用坏现场；stale 记录一并清。"""
        quarantined = []
        for venv in sorted(self.hermes_home.glob("installs/*/environments/*/venv")):
            python = venv_python(venv, self.platform)
            if python.is_file() and self._hermes_import_ok(python):
                continue
            target = venv.with_name(venv.name + ".broken")
            suffix = 0
            while target.exists():
                suffix += 1
                target = venv.with_name(f"{venv.name}.broken{suffix}")
            venv.rename(target)
            quarantined.append(target)
            self.log(f"隔离不可用 Hermes venv：{venv} → {target}")
        recorded = self.hermes_home / "runtime-python.txt"
        if recorded.is_file():
            python = Path(recorded.read_text(encoding="utf-8").strip())
            if not python.is_file() or not self._hermes_import_ok(python):
                recorded.unlink()
        return quarantined

    def _managed_python_ok(self, python: Path) -> bool:
        try:
            self.run_command([python, "-c", "pass"])
            return True
        except InstallError:
            return False

    def quarantine_broken_managed_pythons(self) -> list[Path]:
        """持久树里启动失败的托管 Python 挪边：bootstrap 优先复用现有托管解释器且只查
        可执行位不查能否启动，悬空底座会被当成"就绪"沿用，venv 建在其上重启即崩；
        挪走后 bootstrap 才会重新下载到底座完整的持久目录。"""
        runtime_dir = self.vendor / "hermes-runtime"
        quarantined = []
        for entry in sorted(runtime_dir.glob("python-*")):
            if not entry.is_dir() or ".broken" in entry.name:
                continue
            python = entry / ("python.exe" if self.platform == "win32" else "bin/python3")
            if python.is_file() and self._managed_python_ok(python):
                continue
            target = entry.with_name(entry.name + ".broken")
            suffix = 0
            while target.exists():
                suffix += 1
                target = entry.with_name(f"{entry.name}.broken{suffix}")
            entry.rename(target)
            quarantined.append(target)
            self.log(f"隔离不可用托管 Python：{entry} → {target}")
        return quarantined

    def _materialize_venv_base(self, venv: Path) -> list[Path]:
        """把 venv 底座解释器中指向运行树之外的符号链接条目还原为真实文件。

        解释器启动时按二进制的真实路径向上定位标准库，底座条目被运行树外的
        符号链接顶替后即失效；以同内容真实拷贝还原原位即恢复。其余文件按
        绝对路径加载，不受符号链接影响。
        """
        config = venv / "pyvenv.cfg"
        if not config.is_file():
            return []
        home: Path | None = None
        version = ""
        for line in config.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key, value = key.strip(), value.strip()
            if key == "home":
                home = Path(value).expanduser()
            elif key == "version_info":
                version = value
        if home is None:
            return []
        if not home.is_absolute():
            home = venv / home
        minor = ".".join(version.split(".")[:2])
        if self.platform == "win32":
            names = ["python.exe"]
        else:
            names = list(dict.fromkeys(["python3", "python", f"python{minor}"]))
        runtime_root = (self.vendor / "hermes-runtime").resolve()
        repaired: list[Path] = []
        for name in names:
            entry = home / name
            if not entry.is_symlink():
                continue
            target = entry.resolve(strict=False)
            if not target.is_file() or target.is_relative_to(runtime_root):
                continue
            temporary = entry.with_name(f".{entry.name}.materialize-tmp")
            shutil.copy2(target, temporary)
            os.chmod(temporary, 0o755)
            os.replace(temporary, entry)
            repaired.append(entry)
        return repaired

    def heal_hermes_runtime(self) -> Path | None:
        """Hermes 运行时快速自愈：不可启动的 venv 还原底座解释器后复验，成功返回可用解释器。"""
        healed: Path | None = None
        for venv in sorted(self.hermes_home.glob("installs/*/environments/*/venv")):
            python = venv_python(venv, self.platform)
            if not python.is_file():
                continue
            if self._hermes_import_ok(python):
                healed = healed or python
                continue
            self._materialize_venv_base(venv)
            if python.is_file() and self._hermes_import_ok(python):
                atomic_write(self.hermes_home / "runtime-python.txt", f"{python}\n")
                self.log("Hermes 运行时已自动修复。")
                healed = healed or python
        return healed

    def ensure_hermes_runtime(self) -> Path | None:
        """启动自检：可用即返回；失效先快速自愈；自愈无效且已安装过则重装；未安装返回 None。"""
        python = self.verify_existing_hermes()
        if python is not None:
            return python
        if not (self.hermes_source / ".hermes-bootstrap-complete").is_file():
            return None
        healed = self.heal_hermes_runtime()
        if healed is not None:
            return healed
        self.log("Hermes 运行时无法快速恢复，正在重装。")
        self.install_hermes()
        return self.verify_existing_hermes()

    def hermes_installer_script(self) -> Path:
        filename = "install.ps1" if self.platform == "win32" else "install.sh"
        existing = self.hermes_source / "scripts" / filename
        if existing.is_file():
            text = existing.read_text(encoding="utf-8")
        else:
            url = f"https://raw.githubusercontent.com/NousResearch/hermes-agent/{HERMES_COMMIT}/scripts/{filename}"
            try:
                with urllib.request.urlopen(url, timeout=60) as response:
                    text = response.read().decode("utf-8")
            except Exception as exc:
                raise InstallError(f"下载 Hermes 官方安装器失败：{url}：{exc}") from exc

        if self.platform == "win32":
            original = '        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue'
            guarded = '        try { Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue } catch { }'
            if original in text:
                text = text.replace(original, guarded, 1)
        destination = self.vendor / f".hermes-{filename}"
        atomic_write(destination, text)
        return destination

    def install_hermes(self) -> None:
        python = self.verify_existing_hermes()
        quarantined: list[Path] = []
        if python is None:
            quarantined = self.quarantine_broken_hermes_envs()
            broken_pythons = self.quarantine_broken_managed_pythons()
            self.hermes_home.mkdir(parents=True, exist_ok=True)
            self._install_hermes_official()
            python = self.verify_existing_hermes()
            if python is not None:
                for directory in broken_pythons:
                    shutil.rmtree(directory, ignore_errors=True)
                    self.log(f"已清理隔离的坏托管 Python：{directory}")
        if python is None:
            detail = ("Hermes 安装失败：官方安装器未产出"
                      "可导入 hermes_cli.main 的 runtime venv Python。")
            if quarantined:
                detail += "不可用的旧环境已隔离保留（*.broken），可供诊断。"
            raise InstallError(detail)
        for directory in quarantined:
            shutil.rmtree(directory, ignore_errors=True)
            self.log(f"已清理隔离的坏 Hermes venv：{directory}")
        self.log(f"Hermes runtime：{python}")
        self.log("ffmpeg：" + str(self.configure_ffmpeg()))

    def _install_hermes_official(self) -> None:
        script = self.hermes_installer_script()
        # Hermes 运行时存储（私有 uv + 托管 Python 3.14）默认落 $HERMES_HOME/tools，容器实例的
        # 临时可写层重启/迁移即被清，venv 底座悬空致 encodings 崩。钉到项目持久树：
        # PM store_root() 认 HERMES_RUNTIME_DIR，bootstrap 的 uv python install 认
        # UV_PYTHON_INSTALL_DIR，两者同指持久路径，3.14 底座遂随实例生命周期存活。
        # uv 缓存同样钉持久树并强制 copy 链接：默认 hardlink 会引用缓存/镜像层内的文件，
        # 那些层被清后持久目录里只剩悬空链接，解释器启动即崩。
        runtime_dir = self.vendor / "hermes-runtime"
        env = {
            **os.environ,
            "HERMES_HOME": str(self.hermes_home),
            "HERMES_RUNTIME_DIR": str(runtime_dir),
            "UV_PYTHON_INSTALL_DIR": str(runtime_dir),
            "UV_CACHE_DIR": str(runtime_dir / "uv-cache"),
            "UV_LINK_MODE": "copy",
        }
        if self.platform == "win32":
            shell = shutil.which("powershell") or shutil.which("pwsh")
            if not shell:
                raise InstallError("未找到 PowerShell，无法运行 Hermes 官方安装器。")
            command = [
                shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                "-InstallDir", self.hermes_source, "-Commit", HERMES_COMMIT,
                "-HermesHome", self.hermes_home, "-NonInteractive", "-SkipBrowser",
            ]
        else:
            command = [
                "bash", script, "--dir", self.hermes_source, "--commit", HERMES_COMMIT,
                "--hermes-home", self.hermes_home, "--non-interactive", "--skip-browser",
            ]
        self.run_command(command, env=env)

    def configure_ffmpeg(self) -> Path:
        candidates: list[Path] = []
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if ffmpeg and ffprobe and Path(ffmpeg).parent == Path(ffprobe).parent:
            candidates.append(Path(ffmpeg).parent)
        candidates.extend(sorted(self.hermes_home.glob("tools/ffmpeg-*/bin")))
        # PM 的 ffmpeg 随运行时存储落在 vendor/hermes-runtime；旧落点保留兼容存量安装。
        candidates.extend(sorted((self.vendor / "hermes-runtime").glob("ffmpeg-*/bin")))

        for directory in dict.fromkeys(candidates):
            executable = directory / ("ffmpeg.exe" if self.platform == "win32" else "ffmpeg")
            try:
                result = subprocess.run(
                    [executable, "-version"], capture_output=True, text=True, timeout=15, check=False,
                )
            except OSError:
                continue
            match = re.search(r"ffmpeg version\s+n?(\d+)\.(\d+)", result.stdout)
            if result.returncode == 0 and match and tuple(map(int, match.groups())) >= (5, 1):
                os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
                return directory
        raise InstallError("未找到 ffmpeg/ffprobe ≥5.1；Hermes 工具安装后仍无法自动解析，请安装静态版并加入 PATH。")

    def smoke(self) -> None:
        from scripts.dev_smoke import run_smoke

        python = venv_python(self.mcp_venv, self.platform)
        executable = python.parent / ("viggle-remake-mcp.exe" if self.platform == "win32" else "viggle-remake-mcp")
        worker = self.mcp_source / "tests/fake_worker.py"
        tool_count = run_smoke(python=python, mcp=executable, worker=worker)
        self.log(f"冒烟通过：fake_worker 心跳正常，MCP 发现 {tool_count} 个工具。")

    # ── 阶段 1 后半：前端构建（app/web → dist，start.py 静态服务依赖）──

    def web_inputs(self) -> list[Path]:
        """影响构建产物的输入文件（配置 + src 全量），用于 dist 新鲜度判定。"""
        web = self.root / "app" / "web"
        inputs = [web / name for name in
                  ("package.json", "package-lock.json", "index.html", "tsconfig.json", "vite.config.ts")]
        inputs.extend((web / "src").rglob("*"))
        return [path for path in inputs if path.is_file()]

    def web_dist_current(self) -> bool:
        dist_index = self.root / "app" / "web" / "dist" / "index.html"
        if not dist_index.is_file():
            return False
        built = dist_index.stat().st_mtime
        return all(path.stat().st_mtime <= built for path in self.web_inputs())

    def build_web(self) -> None:
        """npm ci + npm run build（可重入：dist 新于全部输入则跳过；无 lockfile 回落 npm install）。"""
        web = self.root / "app" / "web"
        if not (web / "package.json").is_file():
            raise InstallError(f"前端源码缺失：{web / 'package.json'}；请检查仓库完整性。")
        if self.web_dist_current():
            self.log("前端 dist 已是最新，跳过构建。")
            return
        npm = shutil.which("npm")
        if not npm:
            raise InstallError("未找到 npm：前端构建需要 Node.js ≥18；请安装并加入 PATH 后重跑。")
        if self.dry_run:
            self.log("[dry-run] npm ci && npm run build（app/web），本次未实际执行。")
            return
        install_command = "ci" if (web / "package-lock.json").is_file() else "install"
        self.run_command([npm, install_command], cwd=web)
        self.run_command([npm, "run", "build"], cwd=web)
        if not (web / "dist" / "index.html").is_file():
            raise InstallError(f"前端构建后缺少 dist/index.html：{web / 'dist'}；请检查 npm run build 输出。")
        self.log(f"前端构建完成：{web / 'dist'}")
