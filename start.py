"""H3-Remake 一键启动器（阶段 9 的服务入口，M5.4 补完）：同一进程组内
守护 comfy_worker + 提供 Web 服务（FastAPI 后端 + agent 进程池 + 前端静态服务）。

- worker 守护：worker 在 CUDA error / OOM 后主动退出，由 supervise 循环重启
  （替代原版 run_worker.sh 的 bash 循环）；Web 退出时经 stop 事件带走 worker 子进程；
- Web 服务：create_app(start_pool=True)——任务 API / WS 进度 / 进程池自动领取驱动；
  app/web/dist 已构建时一并提供 SPA 静态服务（未构建仅 API，日志给出构建提示）；
- `uv run start.py`：真实 comfy_worker（ComfyUI venv 解释器跑 remake_worker.py，
   参数取 config.toml [worker]，含 --max-736 175）；
- `uv run start.py --dev`：本地零 GPU，改用队列协议假件 scripts/fake_comfy_worker.py（同一套参数）；
- `--host/--port`：Web 监听地址（缺省 127.0.0.1:8000）。

启动预检：空 api_key/base_url 直接报错不拉起（避免任务领取后才发现缺配）；
ffmpeg ≥5.1 解析（PATH 或 Hermes 工具目录）并注入本进程 PATH——agent 子进程继承，
不依赖安装器进程的临时 PATH；非 --dev 缺 ffmpeg 报错，--dev 仅警告。
Hermes 运行时启动自检：失效自动修复（快速还原无效则重装），无需人工介入。
拉起前把 config.toml 归一写入 Hermes config.yaml/.env（model 三键 + MCP 注册 + 密钥配对）：
缺这一步 Hermes 会用出厂默认模型与端点，且没有 viggle-remake 工具可用。

一键部署口径：install.py 环境安装 + models.py 模型下载 + start.py 启动（双平台手动启动，无开机自启设计）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Sequence

from app.agent.hermes import AgentError, write_hermes_config
from app.server.api import create_app
from scripts.config import Config, ConfigError, load_config, resolve_remake_home
from scripts.installer import InstallError, Installer
from scripts.worker import WorkerError, build_command, supervise

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def check_api_keys(config: Config) -> None:
    """空密钥/空端点预检：agent 链（LLM 决策 + painter 绘制）base_url 与 api_key 须配对齐备。

    端点为空时 Hermes 静默回落出厂默认模型与 OpenRouter 端点，任务领取后才以 404 暴露，
    故启动即报错。
    """
    missing = [f"[{name}].{key}" for name, pairs in
               (("llm", (("base_url", config.llm_base_url), ("api_key", config.llm_api_key))),
                ("painter", (("base_url", config.painter_base_url), ("api_key", config.painter_api_key))))
               for key, value in pairs if not value.strip()]
    if missing:
        raise ConfigError(f"{'、'.join(missing)} 为空：请在 config.toml 填写后重启。")


def ensure_ffmpeg(root: Path) -> Path | None:
    """解析 ffmpeg ≥5.1 并注入本进程 PATH（复用安装器同一套候选/版本校验）；找不到返回 None。"""
    try:
        return Installer(root).configure_ffmpeg()
    except InstallError:
        return None


def ensure_hermes_runtime(root: Path, *, log: Callable[[str], None] = print) -> None:
    """Hermes 运行时启动自检（幂等）：失效自动修复；未安装时不在此报错，交后续注册提示。"""
    try:
        python = Installer(root).ensure_hermes_runtime()
    except InstallError as exc:
        raise AgentError(str(exc)) from exc
    if python is not None:
        log(f"[start] Hermes 运行时就绪：{python}")


def worker_command(
    root: Path,
    *,
    dev: bool,
    config: Config | None = None,
    platform: str | None = None,
) -> list[str]:
    """待守护的 worker 启动命令：真实 remake_worker.py，或 --dev 的协议假件。"""
    if dev:
        config = config or load_config(root)
        home = resolve_remake_home(root)
        return [
            sys.executable, str(root / "scripts" / "fake_comfy_worker.py"),
            "--home", str(home), "--gpu", str(config.worker_gpu),
            "--max-480", str(config.worker_max_480),
            "--max-736", str(config.worker_max_736),
        ]
    return build_command(root, config=config, platform=platform)


def web_app(root: Path, *, log: Callable[[str], None] = print):
    """阶段 9 Web 应用：进程池启用（缺省真实 Hermes × MCP 驱动）+ 前端 dist 静态服务。"""
    dist = root / "app" / "web" / "dist"
    if not (dist / "index.html").is_file():
        log(f"[start] 前端未构建（{dist}）：仅提供 API；"
            "先执行 cd app/web && npm install && npm run build。")
    return create_app(root, start_pool=True, web_dist=dist)


def serve_web(app, *, host: str, port: int, log: Callable[[str], None] = print) -> None:
    """uvicorn 阻塞服务（主线程）；SIGINT 由 uvicorn 优雅收尾后返回。"""
    import uvicorn

    log(f"[start] Web 服务监听 http://{host}:{port}（WS 进度 /ws/progress）。")
    uvicorn.run(app, host=host, port=port, log_level="info")


def supervise_worker(command: Sequence[str], stop: threading.Event, *,
                     log: Callable[[str], None] = print) -> int:
    """线程内守护循环：stop 置位时带走当前 worker 子进程，循环凭 should_stop 退出。

    supervise 的 should_stop 只在子进程退出后检查——watcher 线程负责在 stop 请求时
    terminate 当前子进程，唤醒阻塞中的 wait（spawn 注入口记录进程句柄）。
    """
    holder: dict[str, subprocess.Popen] = {}

    def spawn(cmd: Sequence[str]) -> subprocess.Popen:
        proc = subprocess.Popen(cmd)
        holder["proc"] = proc
        return proc

    def watcher() -> None:
        stop.wait()  # 平时只等 stop，不碰子进程（误 terminate 会触发守护重启死循环）
        # 2s 宽限重试：覆盖 stop 置位瞬间恰逢子进程自然退出/重启的竞态窗口
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            proc = holder.get("proc")
            if proc is not None and proc.poll() is None:
                proc.terminate()  # 带走当前子进程，supervise 凭 should_stop 退出
                return
            time.sleep(0.1)

    threading.Thread(target=watcher, name="worker-stop", daemon=True).start()
    return supervise(command, should_stop=stop.is_set, spawn=spawn, log=log)


def main(
    argv: list[str] | None = None,
    *,
    root: Path | None = None,
    supervise_fn: Callable[[Sequence[str], threading.Event], int] = supervise_worker,
    serve_fn: Callable[..., None] = serve_web,
    log: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(description="启动 H3-Remake 服务（worker 守护 + Web）")
    parser.add_argument("--dev", action="store_true", help="零 GPU 开发模式：以协议假件替代真实 comfy_worker")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Web 监听地址（缺省 {DEFAULT_HOST}）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Web 监听端口（缺省 {DEFAULT_PORT}）")
    args = parser.parse_args(argv)

    root = root or Path(__file__).resolve().parent
    try:
        config = load_config(root)
        check_api_keys(config)
        command = worker_command(root, dev=args.dev, config=config)
    except (WorkerError, ConfigError) as exc:
        print(f"[start] {exc}", file=sys.stderr)
        return 1
    ffmpeg_dir = ensure_ffmpeg(root)
    if ffmpeg_dir is not None:
        log(f"[start] ffmpeg 就绪：{ffmpeg_dir}（已注入 PATH，agent 子进程可用）。")
    elif args.dev:
        log("[start] 警告：未找到 ffmpeg/ffprobe ≥5.1；--dev 假件链路可继续，真实 agent 摄取需要它。")
    else:
        print("[start] 未找到 ffmpeg/ffprobe ≥5.1：请先运行 install.py（阶段 7 会安装）或将其加入 PATH。",
              file=sys.stderr)
        return 1
    # Hermes 运行时自检（失效自动修复）+ 注册（幂等外科手术写入）：
    # model 三键 + MCP 注册 + .env 密钥配对以 config.toml 为准。
    try:
        ensure_hermes_runtime(root, log=log)
        write_hermes_config(root, config=config, log=log)
    except (AgentError, InstallError) as exc:
        print(f"[start] {exc}", file=sys.stderr)
        return 1
    log(f"[start] 守护进程启动：{' '.join(command)}")
    stop = threading.Event()
    worker = threading.Thread(target=supervise_fn, args=(command, stop),
                              name="worker-supervise", daemon=True)
    worker.start()
    code = 0
    try:
        serve_fn(web_app(root, log=log), host=args.host, port=args.port, log=log)
    except KeyboardInterrupt:
        log("[start] 收到中断，worker 与 Web 服务随本进程退出。")
        code = 130
    except ConfigError as exc:
        print(f"[start] config.toml 配置有误：{exc}", file=sys.stderr)
        code = 1
    finally:
        stop.set()
        worker.join(timeout=15)
    return code


if __name__ == "__main__":
    raise SystemExit(main())