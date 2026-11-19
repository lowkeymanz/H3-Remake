"""comfy_worker 启动器（M2.3）：按 config.toml [models].profile 选择 worker 入口脚本（执行层预留，
int8 = 上游 remake_worker.py），定位 ComfyUI venv，按 [worker] 构建启动命令；supervise() 承接原版
run_worker.sh 的循环重启职责（worker 在 CUDA error / OOM 后 os._exit(1)，靠外层守护拉起），
供阶段 9 的 start.py 调用。

真实渲染限云端（M6）；本地队列协议与参数验证走 scripts/fake_comfy_worker.py（同一套协议与上限语义）。
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Callable, Sequence

from scripts.config import Config, ConfigError, load_config, resolve_remake_home
from scripts.installer import InstallError, Installer, venv_python
from scripts.models import ModelError, resolve_profile


class WorkerError(RuntimeError):
    """可向用户直接展示的 worker 启动错误。"""


def resolve_worker(root: Path, *, platform: str | None = None) -> tuple[Path, Path, Path]:
    """→ (档位 worker 脚本, ComfyUI venv python, REMAKE_HOME)；缺件按安装阶段提示修复方式。"""
    try:
        installer = Installer(root, platform=platform)
        profile = resolve_profile(installer.config.models_profile)
        comfy, source = installer.resolve_comfyui()
        if source == "bundled":
            python = venv_python(comfy / "venv", installer.platform)
        else:
            python = installer.external_comfyui_python(comfy)
        home = resolve_remake_home(root)
    except InstallError as exc:
        raise WorkerError(str(exc)) from exc
    except ConfigError as exc:
        raise WorkerError(f"config.toml 配置有误：{exc}") from exc
    except ModelError as exc:
        raise WorkerError(str(exc)) from exc

    script = installer.mcp_source / "comfy_worker" / profile.worker_script
    if not script.is_file():
        raise WorkerError(f"未找到 {script}（档位 {profile.name}）；请先执行阶段 2（原项目源码下载）。")
    if not python.is_file():
        raise WorkerError(f"ComfyUI venv 未就绪（{python}）；请先执行阶段 4（ComfyUI 准备）。")
    return script, python, home


def worker_command(script: Path, python: Path, home: Path, config: Config) -> list[str]:
    """按 remake_worker.py 的参数面构建命令：worker 参数在前，`--` 之后是 ComfyUI flags。"""
    command = [
        str(python), str(script),
        "--gpu", str(config.worker_gpu),
        "--home", str(home),
        "--max-480", str(config.worker_max_480),
        "--max-736", str(config.worker_max_736),
    ]
    if config.worker_comfy_flags:
        command += ["--", *config.worker_comfy_flags]
    return command


def build_command(root: Path, *, config: Config | None = None, platform: str | None = None) -> list[str]:
    """解析路径并构建完整启动命令（start.py 的入口）。"""
    script, python, home = resolve_worker(root, platform=platform)
    config = config or load_config(root)
    return worker_command(script, python, home, config)


def supervise(
    command: Sequence[str],
    *,
    restart_delay: float = 5.0,
    should_stop: Callable[[], bool] | None = None,
    spawn: Callable[[Sequence[str]], subprocess.Popen] = subprocess.Popen,
    log: Callable[[str], None] = print,
) -> int:
    """守护循环（替代原版 run_worker.sh）：子进程退出后按 restart_delay 重启。

    - should_stop 为真时不再拉起新进程（每次退出后检查）；
    - 运行时长超过 restart_delay 的退出立即重启，短于它则先等 restart_delay（避免崩溃死循环）；
    - KeyboardInterrupt / 异常时在 finally 中带走当前子进程；
    返回已发生的退出次数。
    """
    exits = 0
    child: subprocess.Popen | None = None
    try:
        while not (should_stop and should_stop()):
            child = spawn(command)
            started = time.monotonic()
            code = child.wait()
            uptime = time.monotonic() - started
            child = None
            exits += 1
            if should_stop and should_stop():
                log(f"comfy_worker 退出码 {code}（运行 {uptime:.0f}s）；守护停止。")
                break
            log(f"comfy_worker 退出码 {code}（运行 {uptime:.0f}s）；{'%.0f' % restart_delay}s 后重启。")
            if uptime < restart_delay:
                time.sleep(restart_delay)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
    return exits
