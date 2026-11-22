"""渲染基线测量（本地以 fake_comfy_worker 验证同一套逻辑）。

向 $REMAKE_HOME/queue 投 draft(832x480) / final(1280x736) 各档任务（队列协议与真实
worker 完全一致），逐任务：

- 计时取 .done 的 secs（worker 侧口径，含模型加载，不含排队等待）；
- nvidia-smi 采样该卡 memory.used，记录任务窗口内峰值显存（无 nvidia-smi 时记 n/a）；
- 状态：ok / failed（.err，附首行错误）/ timeout（超过单任务时限）。

产出 markdown 基线数据表（--out，默认 $REMAKE_HOME/benchmark/baseline.md）+ 同名 .json 明细。

用法（需先有在线 worker：`uv run start.py`，本地假件 `uv run start.py --dev`）：
  uv run python -m scripts.benchmark --cond 示例源视频.mp4 [--audio 音轨.m4a] \
      [--template 官方示例job.json] [--modes edit,swap] [--frames-480 90,175,277,345] \
      [--frames-736 90,124,175] [--gpu 0] [--out 路径.md] [--timeout 秒] [--sample-every 秒]
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from scripts.config import Config, load_config, resolve_remake_home

MODES = ("swap", "edit")
TIERS = (480, 736)
TIER_LABELS = {480: ("draft", "832x480"), 736: ("final", "1280x736")}
VALID_FRAMES = frozenset(range(90, 346, 17))  # 17n+5，90..345
DEFAULT_FRAMES = {480: (90, 175, 277, 345), 736: (90, 124, 175)}  # 736 档 ≤175（--max-736 175）
DEFAULT_OUT = "benchmark/baseline.md"
HEARTBEAT_FRESH = 90.0  # 心跳新鲜窗口：超过视为无在线 worker


class BenchmarkError(ValueError):
    """可向用户直接展示的基线测量错误。"""


@dataclass(frozen=True)
class Case:
    mode: str
    tier: int
    frames: int


def parse_frames(text: str) -> list[int]:
    try:
        return [int(part) for part in text.replace("，", ",").split(",") if part.strip()]
    except ValueError as exc:
        raise BenchmarkError(f"帧数列表无法解析：{text!r}") from exc


def build_cases(modes: Sequence[str], frames_480: Sequence[int], frames_736: Sequence[int]) -> list[Case]:
    cases = []
    for mode in modes:
        for tier, frames in ((480, frames_480), (736, frames_736)):
            cases.extend(Case(mode, tier, n) for n in frames)
    return cases


def validate_cases(cases: Sequence[Case], config: Config) -> None:
    caps = {480: config.worker_max_480, 736: config.worker_max_736}
    problems = []
    for case in cases:
        if case.mode not in MODES:
            problems.append(f"{case.mode!r}：模式须为 {'/'.join(MODES)}")
        if case.tier not in TIERS:
            problems.append(f"档位 {case.tier}：须为 {'/'.join(map(str, TIERS))}")
        elif case.frames not in VALID_FRAMES:
            problems.append(f"{case.frames} 帧：须为 17n+5 且 90..345")
        elif case.frames > caps[case.tier]:
            problems.append(f"{case.frames} 帧：超出 worker 上限（档 {case.tier} ≤ {caps[case.tier]}）")
    if problems:
        raise BenchmarkError("基线任务清单无效：\n- " + "\n- ".join(problems))


def gpu_probe_factory(
    gpu: int,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Callable[[], dict[str, Any] | None]:
    """构造 nvidia-smi 显存采样器；环境无 nvidia-smi 时返回 None（测量记 n/a）。"""
    if which("nvidia-smi") is None:
        return None

    def probe() -> dict[str, Any] | None:
        try:
            result = run(
                ["nvidia-smi", "-i", str(gpu), "--query-gpu=name,memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        parts = [part.strip() for part in result.stdout.strip().split(",")]
        if len(parts) != 2:
            return None
        try:
            return {"name": parts[0], "used_mib": int(parts[1])}
        except ValueError:
            return None

    return probe


def worker_online(home: Path, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    for beat in (home / "workers").glob("*.json"):
        try:
            if now - json.loads(beat.read_text(encoding="utf-8"))["t"] < HEARTBEAT_FRESH:
                return True
        except (OSError, ValueError, KeyError):
            continue
    return False


def load_template(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchmarkError(f"任务模板无法读取：{path}：{exc}") from exc
    if not isinstance(data, dict):
        raise BenchmarkError(f"任务模板应为 JSON 对象：{path}")
    return data


def submit_job(home: Path, case: Case, name: str, *, cond: Path, audio: Path | None,
               template: dict[str, Any], gpu: int) -> Path:
    job: dict[str, Any] = {
        "mode": case.mode,
        "cond": str(cond),
        "audio": str(audio) if audio else "",
        "refs": [],
        "prompt": "subject_definitions: x | task: y",
        "num_frames": case.frames,
        "seed": 1,
        "out": str(home / "benchmark" / "takes" / f"{name}.mp4"),
        "pin": [],
        "tier": case.tier,
        "gpu": gpu,
    }
    job.update(template)  # 官方示例模板优先（refs/prompt/seed/audio 等按真实样例投递）
    # 基线矩阵的控制项不被模板覆盖：
    job.update({"mode": case.mode, "num_frames": case.frames, "tier": case.tier, "out": job["out"]})
    queue = home / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    path = queue / f"{name}.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    return path


def wait_result(home: Path, name: str, *, timeout: float, sample_every: float,
                 probe: Callable[[], dict[str, Any] | None] | None,
                 poll: float = 0.25) -> dict[str, Any]:
    """等待单个任务出 .done/.err，期间按 sample_every 采样显存；→ {status, secs, vram_gib, error}。"""
    queue = home / "queue"
    stem = queue / name
    deadline = time.monotonic() + timeout
    peak_mib: int | None = None
    next_sample = 0.0
    while time.monotonic() < deadline:
        if stem.with_suffix(".done").is_file():
            info = json.loads(stem.with_suffix(".done").read_text(encoding="utf-8"))
            return {"status": "ok", "secs": float(info.get("secs", 0.0)),
                    "vram_gib": peak_mib / 2**10 if peak_mib is not None else None, "error": None}
        if stem.with_suffix(".err").is_file():
            error = stem.with_suffix(".err").read_text(encoding="utf-8").strip().rsplit("\n", 1)[-1]
            return {"status": "failed", "secs": None,
                    "vram_gib": peak_mib / 2**10 if peak_mib is not None else None, "error": error}
        now = time.monotonic()
        if probe is not None and now >= next_sample:
            sample = probe()
            if sample is not None:
                peak_mib = sample["used_mib"] if peak_mib is None else max(peak_mib, sample["used_mib"])
            next_sample = now + sample_every
        time.sleep(poll)
    return {"status": "timeout", "secs": None,
            "vram_gib": peak_mib / 2**10 if peak_mib is not None else None, "error": None}


def render_report(
    rows: list[dict[str, Any]], *, config: Config, meta: dict[str, Any], out: Path,
) -> None:
    """markdown 数据表 + 同名 .json 明细（增量字段供汇总复用）。"""
    gpu = meta["gpu"]
    lines = [
        "# 渲染基线数据表（4090 48G）",
        "",
        f"- 测量时间：{meta['time']}；主机：{meta['host']}",
        f"- GPU：{gpu if gpu else 'n/a（未采样到 nvidia-smi）'}",
        f"- worker 上限：draft（832x480）≤ {config.worker_max_480} 帧 / "
        f"final（1280x736）≤ {config.worker_max_736} 帧（--max-736 {config.worker_max_736}）",
        f"- 输入：{meta['cond']}"
        + (f"；模板：{meta['template']}" if meta["template"] else "（默认占位任务参数）"),
        "",
        "| # | 模式 | 档位 | 分辨率 | 帧数 | 用时(s) | 峰值显存(GiB) | 状态 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        tier_name, resolution = TIER_LABELS[row["tier"]]
        secs = f"{row['secs']:.1f}" if row["secs"] is not None else "n/a"
        vram = f"{row['vram_gib']:.1f}" if row["vram_gib"] is not None else "n/a"
        lines.append(
            f"| {row['index']} | {row['mode']} | {tier_name} | {resolution} | {row['frames']} "
            f"| {secs} | {vram} | {row['status']} |"
        )
    failures = [row for row in rows if row["status"] != "ok"]
    if failures:
        lines += ["", "## 异常明细", ""]
        lines += [f"- {row['name']}（{row['status']}）：{row['error'] or '—'}" for row in failures]
    lines.append("")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    detail = out.with_suffix(".json")
    detail.write_text(json.dumps({"meta": meta, "rows": rows}, ensure_ascii=False, indent=2),
                      encoding="utf-8")


def run(
    root: Path,
    cases: Sequence[Case],
    *,
    cond: Path,
    audio: Path | None = None,
    template_path: Path | None = None,
    gpu: int | None = None,
    out: Path | None = None,
    timeout: float = 7200.0,
    sample_every: float = 1.0,
    probe_factory: Callable[[], Callable[[], dict[str, Any] | None] | None] | None = None,
    log: Callable[[str], None] = print,
) -> tuple[Path, Path]:
    """执行基线矩阵并产出 (markdown 数据表, json 明细)。"""
    config = load_config(root)
    validate_cases(cases, config)
    home = resolve_remake_home(root)
    if not cond.is_file():
        raise BenchmarkError(f"输入视频不存在：{cond}")
    template = load_template(template_path)
    gpu = config.worker_gpu if gpu is None else gpu
    out = out or home / DEFAULT_OUT

    probe_factory = probe_factory or (lambda: gpu_probe_factory(gpu))
    probe = probe_factory()
    gpu_name = None
    if probe is not None:
        sample = probe()
        if sample is not None:
            gpu_name = sample["name"]
    if not worker_online(home):
        log(f"提示：$REMAKE_HOME（{home}）未检测到新鲜 worker 心跳，任务将持续排队等待。")

    meta = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "host": socket.gethostname(),
        "gpu": gpu_name,
        "cond": str(cond),
        "template": str(template_path) if template_path else None,
    }
    rows: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        name = f"bench_{index:02d}_{case.mode}_{case.tier}_{case.frames}"
        log(f"[bench] {index}/{len(cases)} 投递：{case.mode} / 档 {case.tier} / {case.frames} 帧")
        submit_job(home, case, name, cond=cond, audio=audio, template=template, gpu=gpu)
        result = wait_result(home, name, timeout=timeout, sample_every=sample_every, probe=probe)
        row = {"index": index, "name": name, "mode": case.mode, "tier": case.tier,
               "frames": case.frames, **result}
        rows.append(row)
        log(f"[bench] {name}: {result['status']}"
            + (f"，用时 {result['secs']}s" if result["secs"] is not None else ""))
        if result["error"]:
            log(f"[bench]   {result['error']}")

    render_report(rows, config=config, meta=meta, out=out)
    ok = sum(1 for row in rows if row["status"] == "ok")
    log(f"[bench] 完成 {ok}/{len(rows)} 项；数据表：{out}；明细：{out.with_suffix('.json')}")
    return out, out.with_suffix(".json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="渲染基线测量：draft/final 各档计时 + 显存采样")
    parser.add_argument("--cond", required=True, help="源视频（各档同一输入；官方示例）")
    parser.add_argument("--audio", default=None, help="音轨文件（可缺省，走模板/默认）")
    parser.add_argument("--template", default=None, help="官方示例 job JSON 模板（refs/prompt/seed 等")
    parser.add_argument("--modes", default="edit", help=f"模式列表，逗号分隔（默认 edit；可选 {'/'.join(MODES)}）")
    parser.add_argument("--frames-480", default=",".join(map(str, DEFAULT_FRAMES[480])),
                        help="draft 档帧数列表（17n+5，≤ worker 上限）")
    parser.add_argument("--frames-736", default=",".join(map(str, DEFAULT_FRAMES[736])),
                        help="final 档帧数列表（17n+5，≤175）")
    parser.add_argument("--gpu", type=int, default=None, help="采样显存的卡号（默认 config [worker].gpu）")
    parser.add_argument("--out", default=None, help="数据表输出路径（默认 $REMAKE_HOME/benchmark/baseline.md）")
    parser.add_argument("--timeout", type=float, default=7200.0, help="单任务等待时限秒数（默认 7200）")
    parser.add_argument("--sample-every", type=float, default=1.0, help="显存采样间隔秒数（默认 1）")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent.parent
    modes = [part.strip() for part in args.modes.split(",") if part.strip()]
    try:
        cases = build_cases(modes, parse_frames(args.frames_480), parse_frames(args.frames_736))
        out, _detail = run(
            root, cases,
            cond=Path(args.cond),
            audio=Path(args.audio) if args.audio else None,
            template_path=Path(args.template) if args.template else None,
            gpu=args.gpu,
            out=Path(args.out) if args.out else None,
            timeout=args.timeout,
            sample_every=args.sample_every,
        )
    except BenchmarkError as exc:
        print(f"[bench] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[bench] 测量已中断；已投任务仍会由 worker 完成，重跑前请先清理 queue。", file=sys.stderr)
        return 130
    print(f"[bench] 数据表已生成：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
