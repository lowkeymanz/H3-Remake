"""模型档位（profile）注册表与下载编排——与环境部署严格分离（install.py 只管环境）。

- 组件级注册：权重按组件登记（repo/路径/精确字节/落位子目录），profile 声明组件组合；
  TE/VAE 为跨档位共享组件，切换档位只补下载 diffusion 主模型差分，不重复下载；
- 执行层预留：profile 同时声明 worker 入口脚本（scripts/worker.py 按同一 profile 选择），
  int8 = 上游 remake_worker.py 原样；未来 GGUF 等档位注册新条目 + 适配 worker 即可切换，
  不改架构（GGUF 需不同加载节点与 worker 代码路径，属后续迭代的实体工作）；
- 下载机制沿用原阶段 5 口径：远端 Range 字节核对、dry-run 预演、已有文件按精确字节跳过、
  磁盘动态检查、diffusion 主模型暂存落位（hf-mirror 缺省源，HF_ENDPOINT 可换）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

HF_ENDPOINT_DEFAULT = "https://hf-mirror.com"
STAGING_DIRNAME = ".viggle-staging"
DEFAULT_PROFILE = "int8"
REMOTE_PROBE_ATTEMPTS = 5  # 远端校验瞬时错误（502/超时等）重试次数，退避 2/4/8/16s


class ModelError(RuntimeError):
    """可向用户直接展示的模型下载错误。"""


@dataclass(frozen=True)
class ModelSpec:
    repo: str
    path: str
    size: int
    subdir: str


# 组件注册表：文件名与 comfy_worker/remake_worker.py 固定加载名一致（UNET/TE/VAE）；
# 大小为 HF 实测字节数，下载每次执行都会以远端实际大小复核本清单
COMPONENTS: dict[str, ModelSpec] = {
    "swap_int8": ModelSpec("Viggle/Viggle-Remake", "int8/viggle_remake_swap_pruned_int8_convrot.safetensors",
                           20_970_379_616, "diffusion_models"),
    "edit_int8": ModelSpec("Viggle/Viggle-Remake", "int8/viggle_remake_edit_pruned_int8_convrot.safetensors",
                           20_970_379_616, "diffusion_models"),
    "te_nvfp4": ModelSpec("Comfy-Org/MiniMax-H3", "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                          15_687_142_551, "text_encoders"),
    "vae_video": ModelSpec("Comfy-Org/MiniMax-H3", "vae/minimax_h3_video_vae_fp16.safetensors",
                           5_207_808_496, "vae"),
    "vae_audio": ModelSpec("Comfy-Org/MiniMax-H3", "vae/minimax_h3_audio_vae_fp32.safetensors",
                           605_254_808, "vae"),
}


@dataclass(frozen=True)
class ModelProfile:
    name: str
    description: str
    components: tuple[str, ...]
    worker_script: str  # 执行层预留：comfy_worker 目录下的入口脚本名
    extra_nodes: tuple[str, ...] = ()  # 预留：档位专属节点包（如 GGUF 档的 ComfyUI-GGUF）
    min_ram_gib: float = 0.0  # 档位内存门槛（权重 mmap 驻留）；入口下载前硬检查，0 = 不检查


PROFILES: dict[str, ModelProfile] = {
    DEFAULT_PROFILE: ModelProfile(
        name=DEFAULT_PROFILE,
        description="int8 pruned convrot 定稿档（32GB 显存级消费卡设计，权重 mmap 驻系统内存）",
        components=("swap_int8", "edit_int8", "te_nvfp4", "vae_video", "vae_audio"),
        worker_script="remake_worker.py",
        min_ram_gib=64.0,
    ),
}


def resolve_profile(name: str) -> ModelProfile:
    """按名取档位；未注册即报错并列出可用档位（config 只校验字符串，注册表是唯一事实源）。"""
    profile = PROFILES.get(name)
    if profile is None:
        raise ModelError(f"未知模型档位 {name!r}；可用档位：{', '.join(sorted(PROFILES))}。")
    return profile


def profile_specs(profile: ModelProfile) -> tuple[ModelSpec, ...]:
    """档位 → 权重清单；按 (repo, path) 去重保序（共享组件只下载一次）。"""
    specs: dict[tuple[str, str], ModelSpec] = {}
    for key in profile.components:
        spec = COMPONENTS[key]
        specs.setdefault((spec.repo, spec.path), spec)
    return tuple(specs.values())


class ModelDownloader:
    """按档位下载权重到 $COMFYUI/models/（comfy 路径由入口经 Installer.resolve_comfyui 解析）。"""

    def __init__(
        self,
        comfy: Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        log: Callable[[str], None] = print,
        dry_run: bool = False,
    ) -> None:
        self.comfy = comfy
        self.runner = runner or subprocess.run
        self.log = log
        self.dry_run = dry_run

    def run_command(self, command: Sequence[str | Path], *, env: dict[str, str] | None = None) -> None:
        normalized = [str(part) for part in command]
        self.log(f"执行：{normalized[0]} {normalized[1] if len(normalized) > 1 else ''}")
        try:
            self.runner(normalized, cwd=self.comfy, env=env, check=True)
        except subprocess.CalledProcessError as exc:
            raise ModelError(f"命令退出码 {exc.returncode}：{' '.join(normalized)}") from exc
        except OSError as exc:
            raise ModelError(f"无法执行 {normalized[0]}：{exc}") from exc

    def hf_endpoint(self) -> str:
        return os.environ.get("HF_ENDPOINT") or HF_ENDPOINT_DEFAULT

    def hf_download_env(self) -> dict[str, str]:
        return {**os.environ, "HF_ENDPOINT": self.hf_endpoint(), "UV_HTTP_TIMEOUT": "600"}

    def model_destination(self, spec: ModelSpec) -> Path:
        return self.comfy / "models" / spec.subdir / Path(spec.path).name

    def model_file_ok(self, spec: ModelSpec) -> bool:
        path = self.model_destination(spec)
        try:
            return path.is_file() and path.stat().st_size == spec.size
        except OSError:
            return False

    def verify_model_file(self, spec: ModelSpec) -> None:
        if not self.model_file_ok(spec):
            path = self.model_destination(spec)
            actual = "缺失" if not path.is_file() else f"{path.stat().st_size} 字节"
            raise ModelError(f"模型校验失败：{path} 期望 {spec.size} 字节，实际 {actual}。")

    def remote_model_size(self, spec: ModelSpec) -> int:
        """以 Range 请求取远端文件精确大小并核对清单；大小不符即报错（不下载）。

        重定向（hf-mirror→hf.co→CDN）由 urllib 自动跟随且保留 Range 头；
        镜像网关瞬时故障（502/超时等）按指数退避重试（M6 云端实测 502）。
        """
        url = f"{self.hf_endpoint()}/{spec.repo}/resolve/main/{spec.path}"
        last_error: Exception | None = None
        for attempt in range(REMOTE_PROBE_ATTEMPTS):
            if attempt:
                time.sleep(2 ** attempt)
            request = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    if response.status == 206:
                        header = response.headers.get("Content-Range", "")
                        try:
                            total = int(header.rsplit("/", 1)[-1])
                        except ValueError:
                            raise ModelError(f"无法解析远端 Content-Range（{header!r}）：{url}") from None
                    elif response.status == 200:
                        total = int(response.headers.get("Content-Length", "0"))
                    else:
                        raise ModelError(f"远端响应异常 HTTP {response.status}：{url}")
            except ModelError:
                raise
            except Exception as exc:
                last_error = exc
                continue
            if total != spec.size:
                raise ModelError(f"远端大小与清单不符：{spec.repo}/{spec.path} 期望 {spec.size} 字节，实际 {total}。")
            return total
        raise ModelError(f"远端模型校验失败（已重试 {REMOTE_PROBE_ATTEMPTS} 次）：{url}：{last_error}")

    def download(self, profile: ModelProfile) -> None:
        if not (self.comfy / "main.py").is_file():
            raise ModelError(f"ComfyUI 未就绪（{self.comfy}）；请先执行 install.py（阶段 4 ComfyUI 准备）。")
        specs = profile_specs(profile)
        pending = [spec for spec in specs if not self.model_file_ok(spec)]
        self.log(f"模型档位 {profile.name}：清单 {len(specs)} 项；已就绪 {len(specs) - len(pending)} 项，"
                 f"待下载 {len(pending)} 项。")

        if self.dry_run:
            for spec in specs:
                self.remote_model_size(spec)
            for spec in pending:
                self.log(f"[dry-run] {spec.repo}/{spec.path} → {self.model_destination(spec)}"
                         f"（{spec.size / 2**30:.2f} GiB）")
            total_gib = sum(spec.size for spec in pending) / 2**30
            self.log(f"[dry-run] 远端清单校验通过；待下载合计 {total_gib:.1f} GiB，本次未实际下载。")
            return

        if pending:
            for spec in pending:
                self.remote_model_size(spec)
            models_dir = self.comfy / "models"
            models_dir.mkdir(parents=True, exist_ok=True)
            free_gib = shutil.disk_usage(models_dir).free / 2**30
            need_gib = sum(spec.size for spec in pending) / 2**30 + 5
            if free_gib < need_gib:
                raise ModelError(f"磁盘空间不足：待下载模型约需 {need_gib:.0f} GiB，{models_dir} 所在盘仅剩 {free_gib:.0f} GiB。")

        direct: dict[str, list[ModelSpec]] = {}
        for spec in pending:
            if spec.subdir != "diffusion_models":
                direct.setdefault(spec.repo, []).append(spec)
        for repo, group in direct.items():
            self.run_command(
                ["uvx", "--from", "huggingface_hub", "hf", "download", repo,
                 *(spec.path for spec in group), "--local-dir", self.comfy / "models"],
                env=self.hf_download_env(),
            )
            for spec in group:
                self.verify_model_file(spec)

        staging = self.comfy / "models" / STAGING_DIRNAME
        for spec in (spec for spec in pending if spec.subdir == "diffusion_models"):
            self.run_command(
                ["uvx", "--from", "huggingface_hub", "hf", "download", spec.repo, spec.path, "--local-dir", staging],
                env=self.hf_download_env(),
            )
            staged = staging / spec.path
            destination = self.model_destination(spec)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, destination)
            self.verify_model_file(spec)
        if staging.exists():
            shutil.rmtree(staging)

        ready = sum(1 for spec in specs if self.model_file_ok(spec))
        subdirs = "、".join(dict.fromkeys(spec.subdir for spec in specs))
        self.log(f"模型就绪 {ready}/{len(specs)}（档位 {profile.name}）：{subdirs}。")