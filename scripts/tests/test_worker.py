"""M2.3：comfy_worker 启动命令构造 + 队列协议（以 scripts/fake_comfy_worker.py 假件验证）。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.config import Config
from scripts.installer import venv_python
from scripts.models import ModelProfile
from scripts.worker import build_command, resolve_worker, supervise, WorkerError

FAKE = Path(__file__).resolve().parents[1] / "fake_comfy_worker.py"
HOST = socket.gethostname()
EDIT_SIGMAS = [1.0, 0.983607, 0.96, 0.923077, 0.857143, 0.705882, 0.0]


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class FakeWorkerHarness(unittest.TestCase):
    """隔离 REMAKE_HOME 中拉起协议假件的测试基类。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="m23-protocol-")
        self.home = Path(self._tmp.name) / "home"
        self.queue = self.home / "queue"
        self.procs: list[subprocess.Popen] = []
        self._logs: list = []

    def tearDown(self) -> None:
        for proc in self.procs:
            stop_process(proc)
        for log in self._logs:
            log.close()
        self._tmp.cleanup()

    # ── 基础设施 ──

    def touch(self, relative: str) -> Path:
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def submit(self, name: str, **overrides) -> dict:
        job = {
            "mode": "edit",
            "cond": str(self.touch(f"case/cond_{name}.mp4")),
            "audio": str(self.touch(f"case/audio_{name}.m4a")),
            "refs": [],
            "prompt": "subject_definitions: x | task: y",
            "num_frames": 107,
            "seed": 1,
            "out": str(self.home / "out" / f"{name}.mp4"),
            "pin": [],
            "tier": 480,
        }
        job.update(overrides)
        self.queue.mkdir(parents=True, exist_ok=True)
        (self.queue / f"{name}.json").write_text(json.dumps(job), encoding="utf-8")
        return job

    def stem_path(self, name: str, suffix: str) -> Path:
        return self.queue / f"{name}{suffix}"

    def start(self, *extra: str, gpu: str = "0", max480: int = 345, max736: int = 345) -> subprocess.Popen:
        args = [
            sys.executable, str(FAKE),
            "--home", str(self.home), "--gpu", gpu,
            "--max-480", str(max480), "--max-736", str(max736),
            "--beat-every", "0.2", "--live", "3", "--poll", "0.2",
            *extra,
        ]
        log_path = self.home / f"worker_{gpu}_{len(self.procs)}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("w", encoding="utf-8")
        self._logs.append(log)
        process = subprocess.Popen(
            args, stdout=log, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        self.procs.append(process)
        return process

    def wait_until(self, condition, timeout: float = 20.0, interval: float = 0.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(interval)
        self.fail(f"等待超时（{timeout}s）。worker 日志：\n{self.dump_logs()}")

    def dump_logs(self) -> str:
        parts = []
        for log in self._logs:
            if log.name and Path(log.name).exists():
                parts.append(Path(log.name).read_text(encoding="utf-8", errors="replace"))
        return "\n---\n".join(parts) or "<no logs>"

    def beat_path(self, gpu: str = "0") -> Path:
        return self.home / "workers" / f"{HOST}_{gpu}.json"

    def wait_heartbeat(self, gpu: str = "0") -> dict:
        self.wait_until(lambda: self.beat_path(gpu).is_file())
        return json.loads(self.beat_path(gpu).read_text(encoding="utf-8"))

    def read_done(self, name: str) -> dict:
        return json.loads(self.stem_path(name, ".done").read_text(encoding="utf-8"))


class QueueProtocolTests(FakeWorkerHarness):
    def test_job_claim_done_flow(self) -> None:
        job = self.submit("10_edit")
        worker = self.start()
        done = self.stem_path("10_edit", ".done")
        self.wait_until(lambda: done.is_file())

        info = self.read_done("10_edit")
        self.assertEqual(set(info), {"out", "secs", "host", "gpu", "mode", "sigmas"})
        self.assertEqual(info["out"], job["out"])
        self.assertEqual(info["host"], HOST)
        self.assertEqual(info["gpu"], "0")
        self.assertEqual(info["mode"], "edit")
        self.assertEqual(info["sigmas"], EDIT_SIGMAS)
        self.assertLessEqual(info["secs"], 5.0)

        out = Path(job["out"])
        self.assertTrue(out.is_file())
        self.assertEqual(out.read_bytes(), Path(job["cond"]).read_bytes())  # 占位渲染取 cond 内容
        self.assertFalse(self.stem_path("10_edit", ".json").exists())
        self.assertFalse(self.stem_path("10_edit", ".run").exists())
        self.assertTrue(worker.poll() is None)  # 完成后继续驻守

    def test_failed_job_writes_err_and_worker_survives(self) -> None:
        self.submit("10_bad", pin=[200], refs=[])  # pin 越界（refs 为空时 pin 必须为空）
        self.submit("20_ok")
        self.start()
        self.wait_until(lambda: self.stem_path("20_ok", ".done").is_file())

        err = self.stem_path("10_bad", ".err").read_text(encoding="utf-8")
        self.assertIn("AssertionError", err)
        self.assertFalse(self.stem_path("10_bad", ".run").exists())
        self.assertFalse(self.stem_path("10_bad", ".done").exists())

    def test_frame_and_max736_limits(self) -> None:
        self.submit("10_bad_frames", num_frames=190, tier=480)  # 非 17n+5
        self.submit("20_over_max", num_frames=192, tier=736)     # 超 --max-736 175
        self.submit("30_ok", num_frames=175, tier=736)
        self.start(max736=175)
        self.wait_until(lambda: self.stem_path("30_ok", ".done").is_file())

        self.assertIn("17n+5", self.stem_path("10_bad_frames", ".err").read_text(encoding="utf-8"))
        over = self.stem_path("20_over_max", ".err").read_text(encoding="utf-8")
        self.assertIn("exceeds this worker's max 175 at tier 736", over)
        self.assertFalse(self.stem_path("20_over_max", ".done").exists())
        self.assertTrue((self.home / "out" / "30_ok.mp4").is_file())

    def test_oversized_job_left_to_bigger_worker(self) -> None:
        self.start(gpu="0", max736=175)   # 小卡
        self.start(gpu="1", max736=345)   # 大卡
        self.wait_heartbeat("0")
        self.wait_heartbeat("1")

        self.submit("10_final", num_frames=192, tier=736)
        self.wait_until(lambda: self.stem_path("10_final", ".done").is_file())

        self.assertFalse(self.stem_path("10_final", ".err").exists())  # 小卡没抢去渲失败
        self.assertEqual(self.read_done("10_final")["gpu"], "1")

    def test_orphaned_run_requeued_but_live_claim_untouched(self) -> None:
        # 无心跳点名的孤儿 .run：被在线 worker 放回 .json 并认领
        self.submit("10_orphan")
        orphan = self.stem_path("10_orphan", ".json")
        claimed = self.stem_path("10_orphan", ".run")
        orphan.rename(claimed)
        old = time.time() - 60
        os.utime(claimed, (old, old))

        # 被在线心跳点名的 .run：不得回队
        self.submit("20_owned")
        owned = self.stem_path("20_owned", ".json")
        owned.rename(self.stem_path("20_owned", ".run"))
        os.utime(self.stem_path("20_owned", ".run"), (old, old))
        ghost = self.home / "workers" / "ghosthost_9.json"
        ghost.parent.mkdir(parents=True, exist_ok=True)
        ghost.write_text(json.dumps({
            "t": time.time(), "host": "ghosthost", "gpu": "9", "job": "20_owned",
            "modes": ["swap", "edit"], "max_frames": {"480": 345, "736": 345},
        }), encoding="utf-8")

        self.start("--live", "30")
        self.wait_until(lambda: self.stem_path("10_orphan", ".done").is_file())
        self.assertTrue(self.stem_path("20_owned", ".run").exists())
        self.assertFalse(self.stem_path("20_owned", ".json").exists())

    def test_hop_waits_for_prefix(self) -> None:
        prefix = self.home / "takes" / "p0.mp4"
        self.start()
        self.wait_heartbeat()
        self.submit("30_hop", prefix=str(prefix), prefix_at=0)

        time.sleep(1.0)  # 至少 5 个轮询周期，无人认领
        self.assertTrue(self.stem_path("30_hop", ".json").exists())
        self.assertFalse(self.stem_path("30_hop", ".run").exists())

        self.touch("takes/p0.mp4")
        self.wait_until(lambda: self.stem_path("30_hop", ".done").is_file())

    def test_heartbeat_schema_and_running_job(self) -> None:
        self.start("--delay", "1.5", max736=175)
        heartbeat = self.wait_heartbeat()
        self.assertEqual(set(heartbeat), {"t", "host", "gpu", "pid", "job", "modes", "max_frames"})
        self.assertEqual(heartbeat["host"], HOST)
        self.assertEqual(heartbeat["gpu"], "0")
        self.assertEqual(heartbeat["modes"], ["swap", "edit"])
        self.assertEqual(heartbeat["max_frames"], {"480": 345, "736": 175})  # 键为字符串（MCP 读取口径）
        self.assertIsNone(heartbeat["job"])

        self.submit("50_job")
        self.wait_until(lambda: json.loads(self.beat_path().read_text(encoding="utf-8"))["job"] == "50_job")
        self.wait_until(lambda: self.stem_path("50_job", ".done").is_file())
        self.wait_until(lambda: json.loads(self.beat_path().read_text(encoding="utf-8"))["job"] is None)

    def test_cuda_error_exits_nonzero_for_supervisor(self) -> None:
        self.submit("40_cuda", simulate="cuda_error")
        worker = self.start()
        self.wait_until(lambda: worker.poll() is not None, timeout=15)

        self.assertEqual(worker.returncode, 1)
        self.assertIn("CUDA error", self.stem_path("40_cuda", ".err").read_text(encoding="utf-8"))
        self.assertFalse(self.stem_path("40_cuda", ".run").exists())
        self.assertFalse(self.stem_path("40_cuda", ".done").exists())
        self.assertFalse(Path(self.home / "out" / "40_cuda.mp4").exists())


class SuperviseTests(unittest.TestCase):
    def test_restarts_abnormal_exits_until_stop(self) -> None:
        class FakeProc:
            def wait(self) -> int:
                return 1

            def poll(self) -> int:
                return 1

            def terminate(self) -> None:
                pass

        spawns: list[list[str]] = []

        def spawn(command):
            spawns.append(list(command))
            return FakeProc()

        exits = supervise(
            ["python", "worker.py"], restart_delay=0,
            should_stop=lambda: len(spawns) >= 3, spawn=spawn, log=lambda message: None,
        )
        self.assertEqual(len(spawns), 3)
        self.assertEqual(exits, 3)

    def test_stop_checked_before_first_spawn(self) -> None:
        def spawn(command):  # pragma: no cover - 不应被调用
            raise AssertionError("should_stop 为真时不得拉起进程")

        exits = supervise(["x"], should_stop=lambda: True, spawn=spawn, log=lambda m: None)
        self.assertEqual(exits, 0)


class WorkerCommandTests(unittest.TestCase):
    def make_root(self, tmp: str, *, external: bool = False, worker_toml: str = "") -> Path:
        root = Path(tmp)
        script = root / "vendor/viggle-remake/mcp/comfy_worker/remake_worker.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.touch()
        if external:
            comfy = root / "ext/ComfyUI"
            (comfy / "main.py").parent.mkdir(parents=True, exist_ok=True)
            (comfy / "main.py").touch()
            python = venv_python(comfy / ".venv", "win32")
        else:
            comfy = root / "vendor/ComfyUI"
            comfy.mkdir(parents=True, exist_ok=True)
            python = venv_python(comfy / "venv", "win32")
        python.parent.mkdir(parents=True, exist_ok=True)
        python.touch()
        home = root / "data/home"
        comfy_toml = (
            f'[comfyui]\nmode = "external"\npath = "{comfy.as_posix()}"\n' if external else ""
        )
        (root / "config.toml").write_text(
            f"{comfy_toml}[paths]\nremake_home = \"{home.as_posix()}\"\n\n[worker]\n{worker_toml}",
            encoding="utf-8",
        )
        return root

    def test_bundled_command_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_root(tmp)
            script, python, home = resolve_worker(root, platform="win32")
            command = build_command(root, platform="win32")
            self.assertEqual(command, [
                str(python), str(script),
                "--gpu", "0", "--home", str(home),
                "--max-480", "345", "--max-736", "175",  # 175 为需求定稿默认
            ])

    def test_comfy_flags_go_after_dash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_root(tmp, worker_toml=(
                'gpu = 1\nmax_480 = 300\nmax_736 = 158\n'
                'comfy_flags = ["--vram-headroom", "6"]\n'
            ))
            script, python, home = resolve_worker(root, platform="win32")
            command = build_command(root, platform="win32")
            dash = command.index("--")
            self.assertEqual(command, [
                str(python), str(script),
                "--gpu", "1", "--home", str(home),
                "--max-480", "300", "--max-736", "158",
                "--", "--vram-headroom", "6",
            ])
            self.assertLess(command.index("--max-736"), dash)

    def test_missing_script_and_venv_hint_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_root(tmp)
            script = root / "vendor/viggle-remake/mcp/comfy_worker/remake_worker.py"
            script.unlink()
            with self.assertRaisesRegex(WorkerError, "阶段 2"):
                resolve_worker(root, platform="win32")

        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_root(tmp)
            venv = root / "vendor/ComfyUI/venv/Scripts/python.exe"
            venv.unlink()
            with self.assertRaisesRegex(WorkerError, "阶段 4"):
                resolve_worker(root, platform="win32")

    def test_external_mode_probes_venv_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_root(tmp, external=True)
            script, python, _home = resolve_worker(root, platform="win32")
            expected = root / "ext/ComfyUI/.venv/Scripts/python.exe"
            self.assertEqual(python, expected)

    def test_unknown_profile_reports_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_root(tmp)
            with (root / "config.toml").open("a", encoding="utf-8") as handle:
                handle.write('\n[models]\nprofile = "gguf-q4"\n')
            with self.assertRaisesRegex(WorkerError, "未知模型档位"):
                resolve_worker(root, platform="win32")

    def test_profile_selects_worker_script(self) -> None:
        # 执行层预留：档位声明的 worker_script 决定入口（未来 GGUF 档注册新条目即可切换）
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_root(tmp)
            alt = root / "vendor/viggle-remake/mcp/comfy_worker/gguf_worker.py"
            alt.touch()
            profile = ModelProfile(name="int8", description="测试替身", components=("swap_int8",),
                                   worker_script="gguf_worker.py")
            with patch("scripts.models.PROFILES", {"int8": profile}):
                script, _python, _home = resolve_worker(root, platform="win32")
            self.assertEqual(script, alt)


if __name__ == "__main__":
    unittest.main()
