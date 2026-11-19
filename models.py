"""Viggle Remake 模型下载入口（与 install.py 环境部署严格分离）。

档位由 config.toml [models].profile 决定（缺省 int8 定稿档），--profile 可临时覆盖；
--dry-run 只校验远端清单并输出下载计划，不实际下载。依赖 install.py 阶段 4 的 ComfyUI 就绪；
下载前按档位 min_ram_gib 硬检查系统内存（int8 权重 mmap 驻留需 ≥64 GiB）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts.config import ConfigError
from scripts.installer import InstallError, Installer, total_ram_gib
from scripts.models import ModelDownloader, ModelError, PROFILES, resolve_profile


def main(argv: list[str] | None = None, installer: Installer | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载 Viggle Remake 模型权重（环境安装见 install.py）")
    parser.add_argument("--profile", metavar="档位", help="模型档位（缺省读 config.toml [models].profile；--list 查看可用档位）")
    parser.add_argument("--dry-run", action="store_true", help="只校验远端清单并输出下载计划，不实际下载")
    parser.add_argument("--list", action="store_true", help="列出已注册档位后退出")
    args = parser.parse_args(argv)
    if args.list:
        for profile in PROFILES.values():
            print(f"{profile.name}: {profile.description}")
        return 0

    stage = "模型下载初始化"
    try:
        if installer is None:
            installer = Installer(Path(__file__).resolve().parent)
        profile = resolve_profile(args.profile or installer.config.models_profile)
        stage = f"模型下载（档位 {profile.name}）"
        ram_gib = total_ram_gib()
        if profile.min_ram_gib and ram_gib < profile.min_ram_gib:
            raise ModelError(f"系统内存 {ram_gib:.0f} GiB 低于档位 {profile.name} 所需 {profile.min_ram_gib:.0f} GiB："
                             "权重 mmap 驻留需要更多内存（建议 96 GiB）。")
        comfy, _source = installer.resolve_comfyui()
        downloader = ModelDownloader(comfy, runner=installer.runner, dry_run=args.dry_run)
        downloader.download(profile)
    except KeyboardInterrupt:
        print("\n下载已中断；再次运行同一命令即可继续（已就绪文件按精确字节跳过）。", file=sys.stderr)
        return 130
    except Exception as exc:
        detail = str(exc) if isinstance(exc, (ModelError, InstallError, ConfigError)) else f"{type(exc).__name__}: {exc}"
        print(f"\n[FAIL] {stage}: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())