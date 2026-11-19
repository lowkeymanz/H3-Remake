"""角色库管理（M5.1）：$REMAKE_HOME/chars/ 的 sheet / photo 上传、列表、删除。

与原项目 MCP 的 lib 机制逐字对齐（paint.py sheet(source="lib")）：
- `chars/NAME_sheet.png` —— 角色表（lib 引用必需）；
- `chars/NAME_photo.png` —— 真人照片（可选，paint 时自动拷入 case/chars 供身份保持）；
- `chars/lib_*.json` —— {"chars": {NAME: desc}, "outfits": {NAME: outfit}}，sheet lib
  拷贝时据此补 case.json。本模块管理 lib_chars.json（MCP 按 glob 读全部 lib_*.json，
  不触碰用户自建的其它 lib 文件）。

角色名口径同 MCP：字母/数字/空格/-/_（re.fullmatch(r"[\\w \\-]+")）。上传图经 Pillow
解码校验（非图拒收），统一转 RGB PNG 落位。
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

from PIL import Image

NAME_RE = re.compile(r"[\w \-]+")
LIB_FILE = "lib_chars.json"


class CharsError(ValueError):
    """角色库操作非法（名字不合规 / 非图像 / 角色不存在）。"""


def valid_name(name: str) -> bool:
    return bool(name) and bool(NAME_RE.fullmatch(name))


def _load_lib(chars_dir: Path) -> dict:
    path = chars_dir / LIB_FILE
    if not path.is_file():
        return {"chars": {}, "outfits": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CharsError(f"{LIB_FILE} 不是合法 JSON：{exc}") from exc
    return {"chars": dict(data.get("chars", {})), "outfits": dict(data.get("outfits", {}))}


def _save_lib(chars_dir: Path, lib: dict) -> None:
    (chars_dir / LIB_FILE).write_text(json.dumps(lib, indent=1, ensure_ascii=False), encoding="utf-8")


def _decode_png(data: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise CharsError(f"上传文件不是可解码的图像：{exc}") from exc
    return image.convert("RGB")


def list_chars(chars_dir: Path) -> list[dict]:
    """按 *_sheet.png 扫描角色（MCP lib 引用的口径），附 photo 有无与 lib 描述/服装。"""
    chars_dir = Path(chars_dir)
    lib = _load_lib(chars_dir) if chars_dir.is_dir() else {"chars": {}, "outfits": {}}
    out = []
    for sheet in sorted(chars_dir.glob("*_sheet.png")) if chars_dir.is_dir() else []:
        name = sheet.name[: -len("_sheet.png")]
        out.append({
            "name": name,
            "sheet": sheet.name,
            "photo": f"{name}_photo.png" if (chars_dir / f"{name}_photo.png").is_file() else None,
            "desc": lib["chars"].get(name, ""),
            "outfit": lib["outfits"].get(name, ""),
        })
    return out


def add_sheet(chars_dir: Path, name: str, data: bytes, *, desc: str = "", outfit: str = "") -> dict:
    """上传角色表（覆盖同名）；desc/outfit 给出时写入 lib_chars.json。"""
    if not valid_name(name):
        raise CharsError(f"角色名 {name!r} 不合规：仅字母/数字/空格/-/_")
    image = _decode_png(data)
    chars_dir = Path(chars_dir)
    chars_dir.mkdir(parents=True, exist_ok=True)
    image.save(chars_dir / f"{name}_sheet.png", "PNG")
    if desc or outfit:
        lib = _load_lib(chars_dir)
        if desc:
            lib["chars"][name] = desc
        if outfit:
            lib["outfits"][name] = outfit
        _save_lib(chars_dir, lib)
    return next(c for c in list_chars(chars_dir) if c["name"] == name)


def add_photo(chars_dir: Path, name: str, data: bytes) -> dict:
    """上传角色真人照片（须已有 sheet；paint 时 MCP 自动拷入会话）。"""
    chars_dir = Path(chars_dir)
    if not (chars_dir / f"{name}_sheet.png").is_file():
        raise CharsError(f"角色 {name!r} 不存在：先上传 sheet")
    image = _decode_png(data)
    image.thumbnail((1024, 1024))  # 与 MCP sheet(source="photo") 的照片口径一致
    image.save(chars_dir / f"{name}_photo.png", "PNG")
    return next(c for c in list_chars(chars_dir) if c["name"] == name)


def remove_char(chars_dir: Path, name: str) -> None:
    """删除角色：sheet + photo + lib_chars.json 条目（其它 lib_*.json 不动）。"""
    chars_dir = Path(chars_dir)
    sheet = chars_dir / f"{name}_sheet.png"
    if not sheet.is_file():
        raise CharsError(f"角色 {name!r} 不存在")
    sheet.unlink()
    photo = chars_dir / f"{name}_photo.png"
    if photo.is_file():
        photo.unlink()
    lib = _load_lib(chars_dir)
    if name in lib["chars"] or name in lib["outfits"]:
        lib["chars"].pop(name, None)
        lib["outfits"].pop(name, None)
        _save_lib(chars_dir, lib)