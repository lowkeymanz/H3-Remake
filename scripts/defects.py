"""缺陷样片构造与判定（M4.3）：在 fake 渲染产物上注入已知缺陷，供审查-重试循环回归。

用途：4.3 的"本地注入 fake 产物 / 构造缺陷样片（零 GPU）"——把 fake_worker 的占位成片
（内容 = cond，即与源同帧的合法 mp4）改造成带**已知缺陷**的 take，让 MCP `strip` 的
量测（cut timing / edge bands，kit.py 确定性算法）与 agent 的视觉审查有明确的判定目标；
修复动作（重绘 / seed 1 / pin_before_cuts）触发后，重渲的 take 不带缺陷，审查转 OK。
M6.7 回收的云端真实缺陷 take 与本模块构造的样片共用同一套判定口径（scripts/quality.py）。

缺陷类型（对齐 AGENTS.md "Render, review, finish" 的修复分支）：
- edge_band：某边缘长出黑条（源没有）→ strip 量测 "edge bands"，修复 = 重绘携带坏边的
  关键帧（paint force=true）+ seed 0 再渲；
- cut_early：切点提前 k 帧 → strip "cut fN at fM (-k)" + "early on every seed"，
  修复 = pin_before_cuts + seed 0 再渲（换 seed 无效，AGENTS.md 实测 5/6 仍早）；
- cut_late：切点延后 k 帧 → strip "cut fN at fM (+k)"，修复 = seed 1 再渲、选切点落位的 take；
- cut_drop：切点被吞（前一场画面延续播过切点）→ strip "cut fN dropped"，修复 = seed 1。

实现纪律：仅 ffmpeg/ffprobe + Pillow（项目环境无 numpy）；帧数与尺寸保持不变（只改帧
内容/时序），take 仍是合法 mp4，strip/finish 可正常解码。切点位置在 take 自身上量测
（缩略帧差 + 邻域尖峰比），不依赖会话 cuts.json——构造与判定自洽。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from PIL import Image

FFMPEG = "ffmpeg"   # 调用方负责把 ffmpeg 放进 PATH（Hermes tools 目录或系统安装）
FFPROBE = "ffprobe"
X264 = ["-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p"]
FPS = 24
THUMB = (160, 90)          # 切点量测的缩略尺寸（帧差口径与 kit.find_cuts 的 1/4 缩略同量级）
CUT_THRESHOLD = 22.0       # 平均绝对帧差阈值（kit.find_cuts 同值）
CUT_SPIKE = 3.0            # 邻域尖峰比：切点须显著高于前后 4 帧的中位帧差
EDGE_NAMES = {"top": "top", "bottom": "bottom", "left": "left", "right": "right"}


def probe_video(path: str | Path) -> tuple[int, int, int]:
    """→ (宽, 高, 帧数)（按解码包计数，与 fake_worker/kit 的口径一致）。"""
    raw = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_packets",
         "-show_entries", "stream=width,height,nb_read_packets", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    stream = json.loads(raw)["streams"][0]
    return int(stream["width"]), int(stream["height"]), int(stream["nb_read_packets"])


def read_frames(path: str | Path, size: tuple[int, int] | None = None) -> tuple[int, int, list[bytes]]:
    """解码全部帧为 rgb24 字节列表；size 给定则缩放到该尺寸（切点量测用缩略省内存）。"""
    width, height, _ = probe_video(path)
    if size is not None:
        width, height = size
    command = [FFMPEG, "-v", "error", "-i", str(path)]
    if size is not None:
        command += ["-vf", f"scale={width}:{height}"]
    raw = subprocess.run(command + ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    stride = width * height * 3
    frames = [raw[i:i + stride] for i in range(0, len(raw), stride)]
    return width, height, frames


def write_frames(path: str | Path, width: int, height: int, frames: list[bytes]) -> None:
    """rgb24 帧列表编码回 mp4（24 fps，无音轨——take 的音轨由 finish 从源混入）。"""
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{width}x{height}", "-r", str(FPS), "-i", "-", *X264, str(path)],
        input=b"".join(frames), check=True)


def _mean_abs_diff(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def detect_cuts(path: str | Path) -> list[int]:
    """take 自身的硬切点（新镜头首帧索引）：缩略帧差 > 阈值且 > 邻域中位 × 尖峰比。

    只用于缺陷构造的定位（testsrc 类样片的硬切帧差远超阈值）；审查量测以 MCP kit 为准。
    """
    _, _, frames = read_frames(path, THUMB)
    diffs = [_mean_abs_diff(frames[i], frames[i + 1]) for i in range(len(frames) - 1)]
    cuts = []
    for i, diff in enumerate(diffs):
        neighbours = diffs[max(0, i - 4):i] + diffs[i + 1:i + 5]
        median = sorted(neighbours)[len(neighbours) // 2] if neighbours else 0.0
        if diff > CUT_THRESHOLD and diff > CUT_SPIKE * max(median, 1.0):
            if not cuts or i + 1 - cuts[-1] > 6:   # 相邻候选合并（与 kit.find_cuts 同口径）
                cuts.append(i + 1)
    return cuts


def _splice(frames: list[bytes], cut: int, shift: int) -> list[bytes]:
    """把 cut 处的切点移动 shift 帧（负=提前，正=延后），总帧数不变。

    提前：cut 前 k 帧换成 cut 后的画面（新镜头提前进入），cut 处变为同镜头延续（无跳变），
    尾部补回被覆盖的旧镜头帧；延后：cut 后 k 帧换成 cut 前的画面（旧镜头延续），切点落到
    cut+k。两种情形切点都只移动一次，strip 量测得到单一的 -k / +k 偏移。
    """
    count = len(frames)
    if shift < 0:
        k = -shift
        if not (0 < k < cut):
            raise ValueError(f"cut_early: k={k} 超出可移动范围（cut={cut}, n={count}）")
        # cut-k 起整体取 cut 后的画面（越界钳到末帧）：切点落到 cut-k，尾部为末镜头延续
        return frames[:cut - k] + [frames[min(i + k, count - 1)] for i in range(cut - k, count)]
    k = shift
    if not (cut + k < count):
        raise ValueError(f"cut_late: k={k} 超出可移动范围（cut={cut}, n={count}）")
    out = frames[:cut] + [frames[cut - 1]] * k + frames[cut:]
    return out[:count]


def edge_band(path: str | Path, *, edge: str = "bottom", first: int, last: int,
              thickness: int = 24) -> None:
    """[first, last]（含端点）帧的指定边缘涂黑 thickness 像素——源没有的锐边黑条。

    口径对齐 kit.edge_bands 的判定：边缘起 4+ 行 >15% 黑、随后一行骤降到 <5% 黑、
    黑条深度在画面 35% 以内。thickness 默认 24（480p 的 5%），连续帧数由调用方保证 ≥6。
    """
    if edge not in EDGE_NAMES:
        raise ValueError(f"edge 须为 {'/'.join(EDGE_NAMES)}，实际 {edge!r}")
    width, height, frames = read_frames(path)
    if not (0 <= first <= last < len(frames)):
        raise ValueError(f"帧范围 [{first},{last}] 超出 [0,{len(frames) - 1}]")
    if not (4 <= thickness <= int(0.35 * (height if edge in ("top", "bottom") else width))):
        raise ValueError(f"thickness={thickness} 须 ≥4 且 ≤ 画面该向 35%")
    for index in range(first, last + 1):
        image = Image.frombytes("RGB", (width, height), frames[index])
        if edge == "top":
            box = (0, 0, width, thickness)
        elif edge == "bottom":
            box = (0, height - thickness, width, height)
        elif edge == "left":
            box = (0, 0, thickness, height)
        else:
            box = (width - thickness, 0, width, height)
        image.paste((0, 0, 0), box)
        frames[index] = image.tobytes()
    write_frames(path, width, height, frames)


def cut_shift(path: str | Path, *, frames_early: int = 0, frames_late: int = 0,
              cut_index: int = 0) -> None:
    """把 take 的第 cut_index 个切点提前 frames_early 帧或延后 frames_late 帧。"""
    if bool(frames_early) == bool(frames_late):
        raise ValueError("frames_early 与 frames_late 须恰好一个为正")
    cuts = detect_cuts(path)
    if not cuts or cut_index >= len(cuts):
        raise ValueError(f"take 量测到切点 {cuts}，无第 {cut_index} 个可移动")
    width, height, frames = read_frames(path)
    shift = -frames_early if frames_early else frames_late
    write_frames(path, width, height, _splice(frames, cuts[cut_index], shift))


def cut_drop(path: str | Path, *, cut_index: int = 0) -> None:
    """吞掉第 cut_index 个切点：前一场最后一帧延续播过该镜头（到下一切点或片尾）。

    对齐 kit.cut_drift 的 "dropped" 判定——切点处 take 几乎无跳变（<0.4× 源跳变）。
    """
    cuts = detect_cuts(path)
    if not cuts or cut_index >= len(cuts):
        raise ValueError(f"take 量测到切点 {cuts}，无第 {cut_index} 个可吞")
    width, height, frames = read_frames(path)
    cut = cuts[cut_index]
    end = cuts[cut_index + 1] if cut_index + 1 < len(cuts) else len(frames)
    frozen = frames[cut - 1]
    for index in range(cut, end):
        frames[index] = frozen
    write_frames(path, width, height, frames)


def apply_defect(path: str | Path, spec: dict) -> None:
    """按 spec {"kind": ..., 参数} 注入缺陷（fake_worker 钩子与测试共用入口）。"""
    kind = spec.get("kind")
    if kind == "edge_band":
        edge_band(path, edge=spec.get("edge", "bottom"), first=int(spec["first"]),
                  last=int(spec["last"]), thickness=int(spec.get("thickness", 24)))
    elif kind == "cut_early":
        cut_shift(path, frames_early=int(spec.get("frames", 12)), cut_index=int(spec.get("cut_index", 0)))
    elif kind == "cut_late":
        cut_shift(path, frames_late=int(spec.get("frames", 12)), cut_index=int(spec.get("cut_index", 0)))
    elif kind == "cut_drop":
        cut_drop(path, cut_index=int(spec.get("cut_index", 0)))
    else:
        raise ValueError(f"未知缺陷类型 {kind!r}（edge_band/cut_early/cut_late/cut_drop）")


class DefectPlan:
    """fake_worker 的缺陷注入计划：规则按 (mode, seed, tier) 匹配 + occurrence 计数。

    规则 {"mode": "swap", "seed": 0, "tier": 480, "occurrence": 1, "defect": {...}}：
    第 occurrence 个匹配的 job（1 起）渲完后注入 defect；其余匹配 job 干净——审查-重试
    循环里"首个 take 带缺陷、修复后重渲干净"由此保证。occurrence 缺省 1。
    """

    def __init__(self, rules: list[dict]) -> None:
        self.rules = rules
        self.counts: dict[tuple, int] = {}

    @classmethod
    def load(cls, path: str | Path) -> "DefectPlan":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def next_defect(self, job: dict) -> dict | None:
        key = (job.get("mode"), job.get("seed"), job.get("tier"))
        self.counts[key] = self.counts.get(key, 0) + 1
        occurrence = self.counts[key]
        for rule in self.rules:
            match = all(rule.get(field, job.get(field)) == job.get(field)
                        for field in ("mode", "seed", "tier"))
            if match and int(rule.get("occurrence", 1)) == occurrence:
                return rule["defect"]
        return None