"""Viggle Remake 环境安装入口（不含模型）：完整安装 / --dev 开发模式 / --stages 指定阶段。
模型下载为独立入口 models.py（阶段 5 编号保留，--stages 5 会提示改用 models.py）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts.installer import InstallError, Installer


def main(argv: list[str] | None = None, installer: Installer | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装 Viggle Remake 环境（模型下载见 models.py）")
    parser.add_argument("--dev", action="store_true", help="零 GPU 开发模式：阶段 0/1/2/3/7/8")
    parser.add_argument("--stages", metavar="列表", help="只执行指定阶段（0-8 逗号分隔、不含 5，如 4,6；含 0 时按完整口径自检）")
    parser.add_argument("--dry-run", action="store_true", help="阶段 1 前端构建只输出计划不实际执行（模型下载预演见 models.py --dry-run）")
    args = parser.parse_args(argv)
    if args.dev and args.stages:
        parser.error("--dev 与 --stages 互斥。")
    stages = [part.strip() for part in (args.stages or "").split(",") if part.strip()]
    if args.stages and not stages:
        parser.error("--stages 不能为空。")
    if args.dry_run and stages and "1" not in stages:
        print("提示：--dry-run 仅作用于阶段 1（前端构建），本次执行未包含该阶段。", file=sys.stderr)

    try:
        if installer is None:
            installer = Installer(Path(__file__).resolve().parent, dry_run=args.dry_run)
        if args.dev:
            installer.run_dev()
        elif stages:
            installer.run_stages(stages)
        else:
            installer.run_full()
    except KeyboardInterrupt:
        print("\n安装已中断；再次运行同一命令即可继续。", file=sys.stderr)
        return 130
    except Exception as exc:
        stage = getattr(installer, "current_stage", "安装初始化")
        detail = str(exc) if isinstance(exc, InstallError) else f"{type(exc).__name__}: {exc}"
        print(f"\n[FAIL] {stage}: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
