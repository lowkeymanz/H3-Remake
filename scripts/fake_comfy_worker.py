"""comfy_worker（remake_worker.py）的 GPU-free 队列协议假件（M2.3 测试假件，不部署云端）。

逐段复刻 remake_worker.py 的队列协议，仅"加载模型"与"渲染"两处为占位实现：

- 心跳：$REMAKE_HOME/workers/<host>_<gpu>.json = {t, host, gpu, pid, job, modes, max_frames}，
  max_frames 的键为字符串 "480"/"736"（MCP jobs.py 的读取口径）；
- 认领：queue/*.json 按名排序，先 os.utime 再 rename 为 .run（原子认领，输者得 FileNotFoundError）；
  帧数超过本卡上限但其他在线 worker 可渲的任务跳过（留给它）；hop 等 prefix 文件出现才认领；
- 孤儿回队：.run 的 mtime 超过存活窗口且无在线心跳点名 → 在线 worker 放回 .json；
- 结果：.done（{out, secs, host, gpu, mode, sigmas}；真实 worker 另有 peak_rss_gib，MCP 不读取）
  或 .err（完整 traceback；写后删除 .run）；
- 校验：mode ∈ {swap, edit}、num_frames 为 17n+5 ∈ 90..345、tier ∈ {480, 736}、
  num_frames ≤ --max-<tier>（即 --max-736 175 的上限语义）、refs ≤ 4、pin 与 refs 等长且在帧范围内；
- 退出语义：渲染抛出含 "CUDA error" / "OutOfMemoryError" 的异常 → 写 .err、删 .run、os._exit(1)，
  由外层守护（scripts/worker.py 的 supervise，替代原版 run_worker.sh）重启。

假件专属钩子（真实 worker 没有）：--beat-every/--live/--poll 缩短时序，--delay 渲染耗时，
job["simulate"] = "cuda_error" 注入 CUDA 错误；渲染产物为占位文件（内容取 cond 字节，无 torch/ffmpeg）。
--defect-plan <json>（M4.3）：按 (mode,seed,tier)+occurrence 在渲完后向 take 注入已知缺陷
（scripts/defects.py 的边缘坏条/切点早晚/丢切），供审查-重试循环回归；首 take 带缺陷、修复后重渲干净。
与真实 worker 的已知差异：占位渲染不校验 cond 尺寸/音轨；.done/.err 的写入用 os.replace
（Windows 可重入），语义与原版 rename（Linux）等价。

用法：python scripts/fake_comfy_worker.py --home H [--gpu 0] [--max-480 345] [--max-736 175]
        [--beat-every 10] [--live 120] [--poll 2] [--delay 0] [--defect-plan plan.json]
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

MODES = ("swap", "edit")
FRAMES = range(90, 346, 17)  # 17n + 5, 24 fps
SIGMAS = {"swap": [1.0, 0.990476, 0.965243, 0.923077, 0.8, 0.631579, 0.0],
          "edit": [1.0, 0.983607, 0.96, 0.923077, 0.857143, 0.705882, 0.0]}


def main() -> None:
    parser = argparse.ArgumentParser(description="comfy_worker 队列协议假件")
    parser.add_argument("--home", required=True)
    parser.add_argument("--gpu", default=None, help="卡号标识；默认取 CUDA_VISIBLE_DEVICES 或 0")
    parser.add_argument("--max-480", type=int, default=345, help="832x480 档最长帧数（0 = 不接）")
    parser.add_argument("--max-736", type=int, default=345, help="1280x736 档最长帧数（0 = 不接）")
    parser.add_argument("--beat-every", type=float, default=10.0, help="心跳间隔秒数（假件钩子）")
    parser.add_argument("--live", type=float, default=120.0, help="心跳存活窗口秒数（假件钩子）")
    parser.add_argument("--poll", type=float, default=2.0, help="空闲轮询间隔秒数（假件钩子）")
    parser.add_argument("--delay", type=float, default=0.0, help="每个任务的额外渲染秒数（假件钩子）")
    parser.add_argument("--defect-plan", default=None,
                        help="M4.3 缺陷注入计划 json（假件钩子）：按 mode/seed/tier+occurrence 向 take 注入缺陷")
    args = parser.parse_args()

    defect_plan = None
    if args.defect_plan:
        # 以脚本方式启动时 sys.path[0]=scripts/，需把项目根入路径才能 import scripts.defects
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from scripts.defects import DefectPlan
        defect_plan = DefectPlan.load(args.defect_plan)

    gpu = args.gpu or os.environ.get("CUDA_VISIBLE_DEVICES") or "0"
    host = socket.gethostname()
    home = Path(args.home).expanduser()
    queue, workers = home / "queue", home / "workers"
    for p in (queue, workers):
        p.mkdir(parents=True, exist_ok=True)
    limit = {480: args.max_480, 736: args.max_736}
    state = {"job": None}
    beat_path, lock = workers / f"{host}_{gpu}.json", threading.Lock()

    def beat():
        with lock:
            tmp = beat_path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"t": time.time(), "host": host, "gpu": gpu, "pid": os.getpid(),
                                       "job": state["job"], "modes": list(MODES),
                                       "max_frames": {str(k): v for k, v in limit.items()}}))
            for attempt in range(10):  # Windows：beat 文件被并发读取时 replace 会暂被拒绝，重试即可
                try:
                    tmp.replace(beat_path)  # 原版 rename；replace 保证 Windows 可重入
                    return
                except PermissionError:
                    if attempt == 9:
                        tmp.unlink(missing_ok=True)
                        raise
                    time.sleep(0.05)

    def beat_loop():
        while True:
            try:
                beat()
            except OSError:  # 瞬时文件系统错误不能打断心跳
                pass
            time.sleep(args.beat_every)

    def render(job):
        m, nf, tier, refs = job["mode"], job["num_frames"], job["tier"], job["refs"]
        assert m in MODES, f"mode must be one of {MODES}, got {m!r}"
        assert nf in FRAMES, f"num_frames must be 17n+5 in 90..345, got {nf}"
        assert tier in limit, f"tier must be one of {list(limit)}, got {tier}"
        if nf > limit[tier]:
            raise ValueError(f"num_frames {nf} exceeds this worker's max {limit[tier]} at tier {tier}")
        assert len(refs) <= 4, f"the node pack takes at most 4 refs, got {len(refs)}"
        pin = job.get("pin") or []
        assert len(pin) in (0, len(refs)) and all(0 <= k < nf for k in pin), (pin, len(refs), nf)
        if job.get("simulate") == "cuda_error":  # 假件钩子：验证 CUDA 错误退出语义
            raise RuntimeError("CUDA error: simulated failure injected by fake worker")
        if args.delay:
            time.sleep(args.delay)
        out = Path(job["out"])
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f".{out.stem}.{os.getpid()}.fake")
        cond = Path(job["cond"])  # 占位成片：内容取 cond（无 torch/ffmpeg）
        tmp.write_bytes(cond.read_bytes() if cond.is_file() else b"fake take")
        tmp.replace(out)
        if defect_plan is not None:  # M4.3 钩子：按计划在 take 上注入已知缺陷
            spec = defect_plan.next_defect(job)
            if spec:
                from scripts.defects import apply_defect
                apply_defect(out, spec)
                print(f"{Path(job['out']).stem}: defect injected {spec.get('kind')}", flush=True)

    def live_beats():
        beats = []
        for p in workers.glob("*.json"):
            try:
                hb = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if time.time() - hb["t"] < args.live:
                beats.append(hb)
        return beats

    def requeue_orphans(beats):
        live = {hb["job"] for hb in beats}
        for run in queue.glob("*.run"):
            try:
                if time.time() - run.stat().st_mtime > args.live and run.stem not in live:
                    run.rename(run.with_suffix(".json"))
                    print(f"{run.stem}: orphaned, requeued", flush=True)
            except FileNotFoundError:
                pass

    def next_job(beats):
        others = [hb for hb in beats if (hb["host"], hb["gpu"]) != (host, gpu)]
        for p in sorted(queue.glob("*.json")):
            try:
                job = json.loads(p.read_text())
            except (OSError, ValueError):  # 已被并发认领，或仍在写入
                continue
            if job.get("prefix") and not Path(job["prefix"]).exists():
                continue
            tier, nf = job.get("tier"), job.get("num_frames", 0)
            if nf > limit.get(tier, 0) and any(
                    job.get("mode") in hb["modes"] and hb["max_frames"].get(str(tier), 0) >= nf for hb in others):
                continue  # 本卡渲不动、其他在线 worker 能渲：留给它
            run = p.with_suffix(".run")
            try:  # 先 touch：rename 保留 mtime，超窗且无心跳点名的 .run 才会被回队
                os.utime(p)
                p.rename(run)
            except FileNotFoundError:
                continue
            return run
        return None

    print(f"{host}_{gpu}: fake comfy_worker up; max_frames {limit}", flush=True)
    threading.Thread(target=beat_loop, daemon=True).start()

    while True:
        beats = live_beats()
        requeue_orphans(beats)
        run = next_job(beats)
        if run is None:
            time.sleep(args.poll)
            continue
        state["job"] = run.stem
        try:  # 心跳尽力而为，不阻塞认领（beat_loop 同口径）
            beat()
        except OSError:
            pass
        try:
            job = json.loads(run.read_text())
            t = time.time()
            render(job)
            info = {"out": job["out"], "secs": round(time.time() - t, 1),
                    "host": host, "gpu": gpu, "mode": job["mode"], "sigmas": SIGMAS[job["mode"]]}
            tmp = run.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(info))
            tmp.replace(run.with_suffix(".done"))
            print(f"{run.stem}: {info}", flush=True)
        except Exception:
            tb = traceback.format_exc()
            run.with_suffix(".err").write_text(tb)
            run.unlink(missing_ok=True)
            print(f"{run.stem}: FAILED\n{tb}", flush=True)
            # CUDA 上下文已失效 / OOM 后释放异常张量会中止进程：先退出，由外层守护重启
            if "CUDA error" in tb or "OutOfMemoryError" in tb:
                os._exit(1)
        run.unlink(missing_ok=True)
        state["job"] = None


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
