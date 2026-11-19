"""图像质检（M4.1）：判定 paint / 渲染产物是否为"真图"——尺寸/长宽比合法且非纯色。

用途：4.1 真 painter（MiniMax-H3-Image）回归的"paint 出真图且质检通过"口径——
确认产物是模型真实输出而非失败兜底或空图；后续 M4.3（缺陷样片构造与判定）、
M6（云端 E2E 验收）复用同一套判定。

口径（对齐 M1.2 实测 API 输入/输出约束边界与"真图"画像）：
- 宽高 ∈ [128, 8192] px、长宽比 ≤ 4:1（MiniMax images API 的硬性边界）；
- 灰度标准差 ≥ 15 且采样唯一色数 ≥ 32（纯色/空图 std 为 0，真实照片与生成图远高于此）。

M4.3 扩展：video_edge_bands 对渲染 take 做边缘坏条判定（与 MCP kit.edge_bands 同口径的
独立实现，仅 Pillow）——缺陷样片构造（scripts/defects.py）后的"缺陷确实可见"自动化校验。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageStat

MIN_SIDE = 128
MAX_SIDE = 8192
MAX_ASPECT = 4.0
MIN_STD = 15.0
MIN_COLOURS = 32
SAMPLE_SIDE = 256  # 唯一色采样边长：先缩放采样再统计，避免 4K 大图全量遍历


def image_report(path: str | Path) -> dict:
    """产物指标：宽/高/长宽比/灰度标准差/采样唯一色数（无法解码时抛错）。"""
    with Image.open(path) as image:
        image.load()
        width, height = image.size
        std = ImageStat.Stat(image.convert("L")).stddev[0]
        sample = image.convert("RGB").resize((SAMPLE_SIDE, SAMPLE_SIDE))
        # 唯一色口径用 tobytes 的唯一字节数：纯色图仅 3 个，丰富图数百（避开 Pillow 14 起弃用的 getdata）
        colours = len(set(sample.tobytes()))
    aspect = max(width, height) / max(1, min(width, height))
    return {"width": width, "height": height, "aspect": round(aspect, 2),
            "std": round(std, 1), "colours": colours}


def is_generated_image(path: str | Path) -> bool:
    """质检通过 = 可解码 + 尺寸/长宽比合法 + 非纯色（无法解码的产物一律视为不合格）。"""
    try:
        report = image_report(path)
    except Exception:
        return False
    return (MIN_SIDE <= report["width"] <= MAX_SIDE
            and MIN_SIDE <= report["height"] <= MAX_SIDE
            and report["aspect"] <= MAX_ASPECT
            and report["std"] >= MIN_STD
            and report["colours"] >= MIN_COLOURS)


# ── 视频边缘坏条（M4.3）────────────────────────────────────────────
BAND_DEPTH = 0.35      # 黑条深度上限（占该向画面比例，kit.edge_bands 同值）
BAND_BLACK = 0.15      # 边缘起黑行占比阈值（>15% 黑）
BAND_CLEAR = 0.05      # 黑条内缘骤降阈值（<5% 黑）
BAND_MIN_LINES = 4     # 黑条至少行数
BAND_MIN_RUN = 6       # 连续帧数下限（kit.edge_bands run=6）
BAND_SAMPLE_SIDE = 64  # 边缘剖面的采样宽度（缩略后统计，避免全量像素遍历）


def _band_lines(image: Image.Image, edge: str) -> int:
    """该边缘起的黑条行数：前 4+ 行 >15% 黑、随后一行骤降到 <5% 黑；无则 0。

    剖面取边缘向内 35% 深度、沿边方向缩到 64 采样点；黑 = 像素 max(R,G,B) < 40。
    """
    width, height = image.size
    depth = int(BAND_DEPTH * (height if edge in ("top", "bottom") else width))
    if edge == "top":
        strip = image.crop((0, 0, width, depth)).resize((BAND_SAMPLE_SIDE, depth))
    elif edge == "bottom":
        strip = image.crop((0, height - depth, width, height)).resize((BAND_SAMPLE_SIDE, depth))
        strip = strip.transpose(Image.Transpose.ROTATE_180)
    elif edge == "left":
        strip = image.crop((0, 0, depth, height)).resize((depth, BAND_SAMPLE_SIDE))
        strip = strip.transpose(Image.Transpose.TRANSPOSE)
    else:  # right
        strip = image.crop((width - depth, 0, width, height)).resize((depth, BAND_SAMPLE_SIDE))
        strip = strip.transpose(Image.Transpose.TRANSPOSE).transpose(Image.Transpose.ROTATE_180)
    data = strip.tobytes()
    row = BAND_SAMPLE_SIDE * 3
    black = [sum(1 for k in range(BAND_SAMPLE_SIDE)
                 if max(data[r * row + k * 3:r * row + k * 3 + 3]) < 40) / BAND_SAMPLE_SIDE
             for r in range(depth)]
    lines = 0
    while lines < depth and black[lines] > BAND_BLACK:
        lines += 1
    if lines >= BAND_MIN_LINES and lines < depth and black[lines] < BAND_CLEAR:
        return lines
    return 0


def video_edge_bands(path: str | Path, *, stride: int = 1, src: str | Path | None = None) -> list[str]:
    """take 的边缘坏条报告（"bottom edge band f150-179" 样式，与 MCP strip 量测同口径）。

    逐帧（stride 采样）检查四缘，连续 ≥6 帧出现黑条记一条；无法解码返回空列表。
    src 给定则相对源判定（MCP kit.edge_bands 同口径）：源在同一帧同缘也有黑条则不算缺陷
    （源固有的暗边/letterbox 不误报）；缺省 None 为绝对判定。供缺陷样片构造后的独立校验（不依赖 MCP venv）。
    """
    from scripts.defects import read_frames  # 延迟导入：图像质检不依赖 ffmpeg
    try:
        width, height, frames = read_frames(path)
    except Exception:
        return []
    src_frames, src_size = None, None
    if src is not None:
        try:
            src_size = read_frames(src)
            src_frames = src_size[2]
        except Exception:
            src_frames = None
    runs: dict[str, list[int]] = {e: [] for e in ("top", "bottom", "left", "right")}
    for index in range(0, len(frames), stride):
        image = Image.frombytes("RGB", (width, height), frames[index])
        for edge in runs:
            if not _band_lines(image, edge):
                continue
            if src_frames is not None and index < len(src_frames):
                sw, sh = src_size[0], src_size[1]
                simage = Image.frombytes("RGB", (sw, sh), src_frames[index])
                if _band_lines(simage, edge):
                    continue  # 源也有该坏条：非缺陷（MCP edge_bands 同口径）
            runs[edge].append(index)
    out = []
    for edge, hits in runs.items():
        start = None
        for frame, nxt in zip(hits, hits[1:] + [None]):
            start = frame if start is None else start
            if nxt != frame + stride:
                if start is not None and (frame - start) // stride + 1 >= BAND_MIN_RUN:
                    out.append(f"{edge} edge band f{start}-{frame}")
                start = None
    return out
