"""M2.4/M5.4：start.py 启动器——worker 守护 + 阶段 9 Web 服务入口（uvicorn + SPA 静态服务）。

零费单测：worker 命令构建（真实/--dev 假件）、main 编排（守护线程 + serve 注入、
stop 收尾、中断 130、缺件/坏配置报错）、启动预检（空密钥/空端点报错 + Hermes 注册接线、
ffmpeg PATH 注入）、web_app 组装（进程池启用 + dist 缺省路径）、create_app(web_dist=...)
SPA 静态服务（客户端路由回退/真实文件直出/api 前缀不吞 404/越界路径不泄露 dist 外文件）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agent.hermes import AgentError
from app.server.api import create_app
from scripts.config import Config, ConfigError
from scripts.installer import venv_python
from scripts.worker import build_command
from start import check_api_keys, ensure_ffmpeg, main, supervise_worker, web_app, worker_command


def make_root(tmp: str, *, worker_toml: str = "") -> Path:
    """可解析出真实 worker 的最小项目根（同 test_worker 口径）。"""
    root = Path(tmp)
    script = root / "vendor/viggle-remake/mcp/comfy_worker/remake_worker.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.touch()
    comfy = root / "vendor/ComfyUI"
    comfy.mkdir(parents=True, exist_ok=True)
    for platform in ("win32", "linux"):  # 双平台布局都备，测试不依赖宿主机平台
        python = venv_python(comfy / "venv", platform)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.touch()
    home = root / "data/home"
    (root / "config.toml").write_text(
        f"[paths]\nremake_home = \"{home.as_posix()}\"\n\n[worker]\n{worker_toml}\n"
        '[llm]\napi_key = "test-llm-key"\n\n[painter]\napi_key = "test-paint-key"\n',
        encoding="utf-8",
    )
    return root


class WorkerCommandTests(unittest.TestCase):
    def test_dev_command_uses_fake_worker_with_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, worker_toml="gpu = 1\nmax_480 = 300\nmax_736 = 158\n")
            command = worker_command(root, dev=True)
            self.assertEqual(command, [
                sys.executable, str(root / "scripts" / "fake_comfy_worker.py"),
                "--home", str(root / "data/home"),
                "--gpu", "1", "--max-480", "300", "--max-736", "158",
            ])

    def test_real_command_matches_build_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp, worker_toml="gpu = 0\nmax_480 = 345\nmax_736 = 175\n")
            self.assertEqual(worker_command(root, dev=False, platform="win32"),
                             build_command(root, platform="win32"))


class MainTests(unittest.TestCase):
    """main 编排：守护线程 + serve 注入（不真起 uvicorn），stop 事件收尾。"""

    def _run_main(self, root: Path, argv: list[str], *, serve=None, ffmpeg: Path | None = None):
        supervised: list[list[str]] = []
        stops: list[threading.Event] = []
        served: list[tuple] = []

        def supervise_fn(command, stop):
            supervised.append(list(command))
            stops.append(stop)
            return 0

        def serve_fn(app, *, host, port, log):
            served.append((app, host, port))
            if serve is not None:
                serve()

        with patch("start.ensure_ffmpeg", return_value=ffmpeg), \
                patch("start.write_hermes_config", return_value=(Path("config.yaml"), Path(".env"))):
            code = main(argv, root=root, supervise_fn=supervise_fn, serve_fn=serve_fn,
                        log=lambda _m: None)
        for app, _host, _port in served:
            app.state.store.close()  # 未经 lifespan：手动关库再清 tmp
        return code, supervised, stops, served

    def test_main_dev_supervises_fake_worker_and_serves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            code, supervised, stops, served = self._run_main(root, ["--dev"])
            self.assertEqual(code, 0)
            self.assertEqual(len(supervised), 1)
            self.assertIn("fake_comfy_worker.py", supervised[0][1])
            self.assertEqual(len(served), 1)
            app, host, port = served[0]
            self.assertEqual((host, port), ("127.0.0.1", 8000))
            self.assertIsNotNone(app.state.pool)  # 阶段 9：进程池随入口启用
            self.assertTrue(stops[0].is_set())  # serve 返回后守护收到停止信号

    def test_main_forwards_host_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            code, _sup, _stops, served = self._run_main(
                root, ["--dev", "--host", "0.0.0.0", "--port", "9000"])
            self.assertEqual(code, 0)
            self.assertEqual((served[0][1], served[0][2]), ("0.0.0.0", 9000))

    def test_main_interrupt_returns_130_and_stops_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)

            def interrupt():
                raise KeyboardInterrupt

            code, _sup, stops, _served = self._run_main(root, ["--dev"], serve=interrupt)
            self.assertEqual(code, 130)
            self.assertTrue(stops[0].is_set())

    def test_main_real_mode_reports_missing_pieces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            (root / "vendor/viggle-remake/mcp/comfy_worker/remake_worker.py").unlink()
            code, supervised, _stops, served = self._run_main(root, [])
            self.assertEqual(code, 1)
            self.assertEqual(supervised, [])  # 缺件不拉起守护，也不起 Web
            self.assertEqual(served, [])

    def test_main_bad_config_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            (root / "config.toml").write_text("[llm]\napi_key = 3\n", encoding="utf-8")
            code, supervised, _stops, served = self._run_main(root, ["--dev"])
            self.assertEqual(code, 1)
            self.assertEqual(supervised, [])
            self.assertEqual(served, [])

    def test_main_empty_api_keys_reports_without_starting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            (root / "config.toml").write_text('[llm]\napi_key = ""\n', encoding="utf-8")
            code, supervised, _stops, served = self._run_main(root, ["--dev"])
            self.assertEqual(code, 1)
            self.assertEqual(supervised, [])  # 缺钥不拉起守护，也不起 Web
            self.assertEqual(served, [])

    def test_main_real_mode_requires_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            code, supervised, _stops, served = self._run_main(root, [], ffmpeg=None)
            self.assertEqual(code, 1)
            self.assertEqual(supervised, [])
            self.assertEqual(served, [])

    def test_main_real_mode_serves_with_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            code, supervised, _stops, served = self._run_main(root, [], ffmpeg=Path("ffmpeg-bin"))
            self.assertEqual(code, 0)
            self.assertIn("remake_worker.py", supervised[0][1])
            self.assertEqual(len(served), 1)

    def test_main_registers_hermes_config_from_config_toml(self) -> None:
        """启动即把 config.toml 归一写入 Hermes（model 三键 + MCP 注册 + .env 密钥配对）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            calls: list[tuple] = []

            def register(r, *, config=None, log=print):
                calls.append((r, config))
                return Path("config.yaml"), Path(".env")

            with patch("start.ensure_ffmpeg", return_value=None), \
                    patch("start.write_hermes_config", side_effect=register):
                code = main(["--dev"], root=root, supervise_fn=lambda command, stop: 0,
                            serve_fn=lambda app, **kwargs: app.state.store.close(),
                            log=lambda _m: None)
            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], root)
            self.assertEqual(calls[0][1].llm_api_key, "test-llm-key")

    def test_main_hermes_registration_failure_reports_without_starting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            supervised: list = []
            served: list = []

            def supervise_fn(command, stop):
                supervised.append(list(command))
                return 0

            def serve_fn(app, *, host, port, log):
                served.append(app)

            with patch("start.ensure_ffmpeg", return_value=None), \
                    patch("start.write_hermes_config", side_effect=AgentError("未找到 Hermes config.yaml")):
                code = main(["--dev"], root=root, supervise_fn=supervise_fn, serve_fn=serve_fn,
                            log=lambda _m: None)
            self.assertEqual(code, 1)
            self.assertEqual(supervised, [])  # 注册失败不拉起守护，也不起 Web
            self.assertEqual(served, [])


class PreflightTests(unittest.TestCase):
    """启动预检：空密钥报错面 + ffmpeg 解析/PATH 注入（复用安装器校验，零下载零费）。"""

    def test_check_api_keys_reports_missing_sections(self) -> None:
        with self.assertRaisesRegex(ConfigError, r"\[llm\]\.api_key.*\[painter\]\.api_key"):
            check_api_keys(Config())
        with self.assertRaisesRegex(ConfigError, r"\[painter\]\.api_key"):
            check_api_keys(Config(llm_api_key="k"))
        with self.assertRaisesRegex(ConfigError, r"\[llm\]\.api_key"):
            check_api_keys(Config(llm_api_key="   ", painter_api_key="k"))  # 空白视同缺钥
        check_api_keys(Config(llm_api_key="k1", painter_api_key="k2"))  # 齐备不报错

    def test_check_api_keys_reports_empty_base_url(self) -> None:
        """端点为空同样启动即拦：否则 Hermes 静默回落出厂默认模型与 OpenRouter 端点。"""
        with self.assertRaisesRegex(ConfigError, r"\[llm\]\.base_url"):
            check_api_keys(Config(llm_api_key="k1", painter_api_key="k2", llm_base_url=""))
        with self.assertRaisesRegex(ConfigError, r"\[painter\]\.base_url"):
            check_api_keys(Config(llm_api_key="k1", painter_api_key="k2", painter_base_url="  "))

    def _tools_dir(self, root: Path) -> Path:
        bindir = root / "data/home/hermes/tools/ffmpeg-6.0/bin"
        bindir.mkdir(parents=True)
        (bindir / "ffmpeg").touch()  # 双平台文件名都备，测试不依赖宿主机平台
        (bindir / "ffmpeg.exe").touch()
        return bindir

    def test_ensure_ffmpeg_injects_hermes_tools_into_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            bindir = self._tools_dir(root)
            version = subprocess.CompletedProcess([], 0, stdout="ffmpeg version 6.0 Copyright (c) 2000-2023")
            with patch("scripts.installer.shutil.which", return_value=None), \
                    patch("scripts.installer.subprocess.run", return_value=version), \
                    patch.dict(os.environ, {"PATH": "orig-path"}):
                self.assertEqual(ensure_ffmpeg(root), bindir)
                self.assertTrue(os.environ["PATH"].startswith(str(bindir) + os.pathsep))
                self.assertIn("orig-path", os.environ["PATH"])  # 原 PATH 保留在后

    def test_ensure_ffmpeg_missing_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            with patch("scripts.installer.shutil.which", return_value=None):
                self.assertIsNone(ensure_ffmpeg(root))

    def test_ensure_ffmpeg_rejects_old_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            self._tools_dir(root)
            version = subprocess.CompletedProcess([], 0, stdout="ffmpeg version 4.4.2 Copyright (c) 2000-2021")
            with patch("scripts.installer.shutil.which", return_value=None), \
                    patch("scripts.installer.subprocess.run", return_value=version), \
                    patch.dict(os.environ, {"PATH": "orig-path"}):
                self.assertIsNone(ensure_ffmpeg(root))
                self.assertEqual(os.environ["PATH"], "orig-path")  # 不达标不注入


class SuperviseWorkerTests(unittest.TestCase):
    """真实守护停止路径：stop 置位 → watcher 带走子进程 → 循环退出（心跳文件判存活）。"""

    def test_stop_terminates_child_and_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            beat = Path(tmp) / "beat"
            child = ("import pathlib, time\n"
                     f"p = pathlib.Path(r'{beat}')\n"
                     "while True:\n"
                     "    p.write_text('x')\n"
                     "    time.sleep(0.1)\n")
            stop = threading.Event()
            result: list[int] = []
            thread = threading.Thread(
                target=lambda: result.append(supervise_worker(
                    [sys.executable, "-c", child], stop, log=lambda _m: None)),
                daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not beat.exists():
                time.sleep(0.05)
            self.assertTrue(beat.exists(), "worker 子进程未拉起")
            stamp = beat.stat().st_mtime_ns
            time.sleep(0.8)
            self.assertGreater(beat.stat().st_mtime_ns, stamp)  # 平时守护不碰子进程（无误 terminate）
            stop.set()
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())  # 守护循环已退出
            self.assertEqual(len(result), 1)
            stamp = beat.stat().st_mtime_ns
            time.sleep(0.5)
            self.assertEqual(beat.stat().st_mtime_ns, stamp)  # 心跳停摆：子进程已被带走


class WebAppTests(unittest.TestCase):
    """web_app：进程池启用 + 前端 dist 缺省路径（未构建给提示，仅 API）。"""

    def test_web_app_enables_pool_and_serves_dist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            dist = root / "app" / "web" / "dist"
            (dist / "assets").mkdir(parents=True)
            (dist / "index.html").write_text("<html>spa-index</html>", encoding="utf-8")
            logs: list[str] = []
            app = web_app(root, log=logs.append)
            self.assertIsNotNone(app.state.pool)
            self.assertFalse(any("前端未构建" in line for line in logs))
            with TestClient(app) as client:
                self.assertIn("spa-index", client.get("/").text)
                self.assertIn("spa-index", client.get("/tasks/tabc").text)

    def test_web_app_missing_dist_logs_hint_api_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(tmp)
            logs: list[str] = []
            app = web_app(root, log=logs.append)
            self.assertTrue(any("前端未构建" in line for line in logs))
            with TestClient(app) as client:
                self.assertEqual(client.get("/").status_code, 404)
                self.assertEqual(client.get("/api/tasks").json(), [])


class SpaTests(unittest.TestCase):
    """create_app(web_dist=...)：SPA 回退/真实文件直出/前缀保护/越界不泄露。"""

    def setUp(self) -> None:
        self._stack = ExitStack()
        self.addCleanup(self._stack.close)
        self._tmp = tempfile.TemporaryDirectory()
        self._stack.callback(self._tmp.cleanup)
        self.root = make_root(self._tmp.name)
        self.dist = Path(self._tmp.name) / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "index.html").write_text("<html>spa-index</html>", encoding="utf-8")
        (self.dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
        self.client = self._stack.enter_context(
            TestClient(create_app(self.root, web_dist=self.dist)))

    def test_root_and_client_routes_fall_back_to_index(self) -> None:
        for path in ("/", "/tasks/tabc123", "/chars", "/settings"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("spa-index", response.text)

    def test_real_assets_served_directly(self) -> None:
        response = self.client.get("/assets/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "console.log(1)")

    def test_api_and_files_prefix_not_swallowed(self) -> None:
        response = self.client.get("/api/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "not found")
        self.assertEqual(self.client.get("/files/sessions/none.png").status_code, 404)

    def test_traversal_never_leaks_outside_dist(self) -> None:
        response = self.client.get("/%2e%2e/config.toml")
        self.assertNotIn("remake_home", response.text)  # 越界路径回退 index，不泄露配置
        self.assertIn("spa-index", response.text)

    def test_without_dist_root_is_404(self) -> None:
        client = self._stack.enter_context(TestClient(create_app(self.root)))
        self.assertEqual(client.get("/").status_code, 404)
        self.assertEqual(client.get("/api/tasks").json(), [])


if __name__ == "__main__":
    unittest.main()