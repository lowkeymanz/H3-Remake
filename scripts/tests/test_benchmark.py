"""M2.4：scripts/benchmark.py 基线测量（以 fake_comfy_worker 假件验证投递/计时/采样/数据表）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scripts.benchmark import (
    BenchmarkError,
    Case,
    build_cases,
    gpu_probe_factory,
    parse_frames,
    run,
    worker_online,
)

FAKE = Path(__file__).resolve().parents[1] / "fake_comfy_worker.py"


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class FakeWorkerBenchmarkHarness(unittest.TestCase):
    """隔离 REMAKE_HOME + 假 worker；项目根仅含 config.toml。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="m24-bench-", ignore_cleanup_errors=True)
        tmp = Path(self._tmp.name)
        self.root = tmp / "root"
        self.home = tmp / "home"
        self.cond = tmp / "case" / "source.mp4"
        self.cond.parent.mkdir(parents=True)
        self.cond.write_bytes(b"source-video")
        self.root.mkdir()
        (self.root / "config.toml").write_text(
            f"[paths]\nremake_home = \"{self.home.as_posix()}\"\n\n[worker]\nmax_736 = 175\n",
            encoding="utf-8",
        )
        self.procs: list[subprocess.Popen] = []
        self._logs: list = []

    def tearDown(self) -> None:
        for proc in self.procs:
            stop_process(proc)
        for log in self._logs:
            log.close()
        self._tmp.cleanup()

    def start_worker(self, *extra: str, max736: int = 175) -> subprocess.Popen:
        args = [
            sys.executable, str(FAKE),
            "--home", str(self.home), "--gpu", "0",
            "--max-480", "345", "--max-736", str(max736),
            "--beat-every", "0.2", "--live", "3", "--poll", "0.2",
            *extra,
        ]
        self.home.mkdir(parents=True, exist_ok=True)
        log = (self.home / f"worker_{len(self.procs)}.log").open("w", encoding="utf-8")
        self._logs.append(log)
        process = subprocess.Popen(
            args, stdout=log, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        self.procs.append(process)
        return process

    def wait_until(self, condition, timeout: float = 20.0, interval: float = 0.05) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(interval)
        self.fail("等待超时。")


class RunBenchmarkTests(FakeWorkerBenchmarkHarness):
    def test_report_rows_with_probe(self) -> None:
        self.start_worker("--delay", "0.5")
        logs: list[str] = []
        cases = [Case("edit", 480, 107), Case("swap", 736, 175)]
        out, detail = run(
            self.root, cases, cond=self.cond,
            probe_factory=lambda: (lambda: {"name": "Fake GPU", "used_mib": 2048}),
            sample_every=0.05, timeout=30, log=logs.append,
        )

        self.assertEqual(out, self.home / "benchmark" / "baseline.md")
        self.assertEqual(detail, self.home / "benchmark" / "baseline.json")
        self.assertTrue((self.home / "benchmark" / "takes" / "bench_01_edit_480_107.mp4").is_file())
        self.assertTrue((self.home / "benchmark" / "takes" / "bench_02_swap_736_175.mp4").is_file())

        text = out.read_text(encoding="utf-8")
        self.assertIn("渲染基线数据表（4090 48G）", text)
        self.assertIn("Fake GPU", text)
        self.assertIn("| 1 | edit | draft | 832x480 | 107 |", text)
        self.assertIn("| 2 | swap | final | 1280x736 | 175 |", text)
        self.assertIn("2.0 | ok |", text)  # 2048 MiB → 2.0 GiB
        self.assertNotIn("异常明细", text)  # 全部 ok

        rows = json.loads(detail.read_text(encoding="utf-8"))["rows"]
        self.assertEqual([row["status"] for row in rows], ["ok", "ok"])
        self.assertEqual([row["vram_gib"] for row in rows], [2.0, 2.0])
        self.assertTrue(all(row["secs"] >= 0.5 for row in rows))  # .done 计时含 --delay
        self.assertTrue(any("完成 2/2 项" in line for line in logs))

    def test_probe_absent_marks_na(self) -> None:
        self.start_worker()
        out, _detail = run(
            self.root, [Case("edit", 480, 107)], cond=self.cond,
            probe_factory=lambda: None, timeout=30, log=lambda _m: None,
        )
        text = out.read_text(encoding="utf-8")
        self.assertIn("n/a（未采样到 nvidia-smi）", text)
        self.assertIn("| n/a | ok |", text)

    def test_failed_job_marked_and_continues(self) -> None:
        self.start_worker("--max-736", "90")  # worker 实际上限低于 config：任务会在 worker 侧失败
        out, detail = run(
            self.root, [Case("edit", 736, 124), Case("edit", 480, 90)], cond=self.cond,
            probe_factory=lambda: None, timeout=30, log=lambda _m: None,
        )
        text = out.read_text(encoding="utf-8")
        self.assertIn("failed", text)
        self.assertIn("bench_01_edit_736_124", text)
        self.assertIn("exceeds this worker's max 90", text)
        self.assertIn("| 2 | edit | draft | 832x480 | 90 |", text)  # 后续任务继续
        rows = json.loads(detail.read_text(encoding="utf-8"))["rows"]
        self.assertEqual([row["status"] for row in rows], ["failed", "ok"])

    def test_timeout_without_worker(self) -> None:
        logs: list[str] = []
        out, _detail = run(
            self.root, [Case("edit", 480, 90)], cond=self.cond,
            probe_factory=lambda: None, timeout=0.8, log=logs.append,
        )
        self.assertIn("timeout", out.read_text(encoding="utf-8"))
        self.assertTrue((self.home / "queue" / "bench_01_edit_480_90.json").is_file())  # 任务留在队列
        self.assertTrue(any("未检测到新鲜 worker 心跳" in line for line in logs))


class ValidationTests(FakeWorkerBenchmarkHarness):
    def test_invalid_cases_rejected_before_submitting(self) -> None:
        cases = [Case("edit", 480, 100), Case("swap", 736, 192), Case("bogus", 480, 107)]
        with self.assertRaisesRegex(BenchmarkError, "17n\\+5"):
            run(self.root, cases, cond=self.cond, log=lambda _m: None)
        self.assertFalse((self.home / "queue").exists())  # 未投递任何任务

    def test_over_max736_rejected_against_config(self) -> None:
        with self.assertRaisesRegex(BenchmarkError, "超出 worker 上限"):
            run(self.root, [Case("edit", 736, 192)], cond=self.cond, log=lambda _m: None)

    def test_missing_cond_rejected(self) -> None:
        with self.assertRaisesRegex(BenchmarkError, "输入视频不存在"):
            run(self.root, [Case("edit", 480, 107)], cond=self.root / "nope.mp4",
                log=lambda _m: None)


class UnitsTests(unittest.TestCase):
    def test_parse_frames(self) -> None:
        self.assertEqual(parse_frames("90, 175，345"), [90, 175, 345])
        with self.assertRaises(BenchmarkError):
            parse_frames("90,x")

    def test_build_cases_matrix(self) -> None:
        cases = build_cases(["swap", "edit"], [90], [90, 175])
        self.assertEqual(cases, [
            Case("swap", 480, 90), Case("swap", 736, 90), Case("swap", 736, 175),
            Case("edit", 480, 90), Case("edit", 736, 90), Case("edit", 736, 175),
        ])

    def test_default_frames_are_valid(self) -> None:
        from scripts.benchmark import DEFAULT_FRAMES, VALID_FRAMES

        for tier, frames in DEFAULT_FRAMES.items():
            for frames_ in [frames]:  # noqa: B007
                for n in frames_:
                    self.assertIn(n, VALID_FRAMES)
        self.assertEqual(max(DEFAULT_FRAMES[736]), 175)  # --max-736 175

    def test_gpu_probe_factory(self) -> None:
        self.assertIsNone(gpu_probe_factory(0, which=lambda _n: None))

        def run_ok(*_a, **_k):
            return subprocess.CompletedProcess([], 0, stdout="Fake GPU, 12345")

        probe = gpu_probe_factory(1, which=lambda _n: "nvidia-smi", run=run_ok)
        self.assertEqual(probe(), {"name": "Fake GPU", "used_mib": 12345})

        def run_fail(*_a, **_k):
            return subprocess.CompletedProcess([], 1, stdout="")

        self.assertIsNone(gpu_probe_factory(1, which=lambda _n: "nvidia-smi", run=run_fail)())

    def test_worker_online_heartbeat_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            beat_dir = home / "workers"
            beat_dir.mkdir()
            self.assertFalse(worker_online(home))
            beat_dir.joinpath("host_0.json").write_text(
                json.dumps({"t": time.time() - 1000}), encoding="utf-8")
            self.assertFalse(worker_online(home))  # 过期不算在线
            beat_dir.joinpath("host_0.json").write_text(
                json.dumps({"t": time.time()}), encoding="utf-8")
            self.assertTrue(worker_online(home))


if __name__ == "__main__":
    unittest.main()
