"""设置读写（M5.3 页面 4）：config.toml [llm]/[painter]/[budget] 外科手术式编辑。

- 读：api_key 掩码（只回是否已配置，不回显真密钥）；路径只读展示；
- 写：只改显式给出的键，api_key 传空串表示保留现场密钥（前端掩码不回传）；
  逐行替换/追加，保留注释与其它段（与 M3.1 Hermes 配置外科手术写入同纪律），
  写后 load_config 复核，坏配置不落盘。
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.config import ConfigError, load_config

API_KEYS = ("base_url", "api_key", "model")
SECTION_KEYS = {"llm": API_KEYS, "painter": API_KEYS, "budget": ("max_images",)}


def read_settings(root: Path) -> dict:
    """当前设置视图（api_key 掩码为布尔）。"""
    cfg = load_config(Path(root))
    return {
        "llm": {"model": cfg.llm_model, "base_url": cfg.llm_base_url,
                "api_key_set": bool(cfg.llm_api_key)},
        "painter": {"model": cfg.painter_model, "base_url": cfg.painter_base_url,
                    "api_key_set": bool(cfg.painter_api_key)},
        "budget_default": cfg.budget_max_images,
        "remake_home": str(cfg.remake_home or ""),
    }


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _validate(updates: dict) -> dict:
    """只认 llm/painter/budget 三段与其已知键；类型不符抛 ConfigError。"""
    if not isinstance(updates, dict):
        raise ConfigError("设置须为对象。")
    clean: dict = {}
    for section, body in updates.items():
        if section not in SECTION_KEYS:
            raise ConfigError(f"不支持的设置段 {section!r}（{'/'.join(SECTION_KEYS)}）。")
        if not isinstance(body, dict):
            raise ConfigError(f"[{section}] 应为对象。")
        picked = {}
        for key, value in body.items():
            if key not in SECTION_KEYS[section]:
                raise ConfigError(f"不支持的设置键 {section}.{key}。")
            if section == "budget":
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ConfigError(f"budget.max_images 应为正整数，实际 {value!r}。")
            elif not isinstance(value, str):
                raise ConfigError(f"{section}.{key} 应为字符串，实际 {value!r}。")
            if key == "api_key" and value == "":
                continue  # 空密钥 = 保留现场（掩码视图不回传真密钥）
            picked[key] = value
        if picked:
            clean[section] = picked
    return clean


def _section_span(lines: list[str], name: str) -> tuple[int, int] | None:
    """[name] 段的行区间 [头行, 下一段头行)；不存在返回 None。"""
    header = re.compile(rf"^\s*\[{re.escape(name)}\]\s*(#.*)?$")
    start = next((i for i, line in enumerate(lines) if header.match(line)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^\s*\[", lines[i])), len(lines))
    return start, end


def update_settings(root: Path, updates: dict) -> dict:
    """外科手术式写入 config.toml，返回更新后的设置视图。"""
    root = Path(root)
    clean = _validate(updates)
    path = root / "config.toml"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = text.splitlines()
    for section, picked in clean.items():
        span = _section_span(lines, section)
        if span is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"[{section}]")
            span = (len(lines) - 1, len(lines))
        start, end = span
        for key, value in picked.items():
            rendered = str(value) if section == "budget" else _toml_str(value)
            key_re = re.compile(rf"^(\s*){re.escape(key)}\s*=")
            hit = next((i for i in range(start + 1, end) if key_re.match(lines[i])), None)
            if hit is not None:
                lines[hit] = f"{key} = {rendered}"
            else:
                lines.insert(end, f"{key} = {rendered}")
                end += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        load_config(root)  # 复核：坏配置立即暴露（理论上不会，双保险）
    except ConfigError:
        path.write_text(text, encoding="utf-8")
        raise
    return read_settings(root)