"""读取 config.toml（缺失时按模板默认值）。config.toml 由用户从 config.example.toml 复制，
阶段 7 的生成引导属后续里程碑，当前不自动生成。"""

from __future__ import annotations

import os

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from dataclasses import dataclass
from pathlib import Path

COMFYUI_MODES = ("bundled", "external", "auto")


class ConfigError(ValueError):
    """config.toml 内容无法使用。"""


@dataclass(frozen=True)
class Config:
    comfyui_mode: str = "bundled"
    comfyui_path: str = ""
    remake_home: str | None = None
    worker_gpu: int = 0
    worker_max_480: int = 345
    worker_max_736: int = 175  # 需求定稿：final 档初版单窗上限（--max-736 175）
    worker_comfy_flags: tuple[str, ...] = ()
    # [llm]（M1.3 定版：百炼 qwen3.7-plus）与 [painter]（M1.2 定版：MiniMax-H3-Image）
    # 由 agent 层读取（M3.1：Hermes 注册与 .env 生成）
    llm_model: str = "qwen3.7-plus"
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key: str = ""
    painter_model: str = "MiniMax-H3-Image"
    painter_base_url: str = "https://api.minimax.cn/v1"
    painter_api_key: str = ""
    # [budget].max_images：预算默认值（M5.3 设置页；任务创建表单预填，写入 per-session paint_limit）
    budget_max_images: int = 24
    # [agent].round_timeout：单轮 agent 子进程超时秒数（真渲染分钟至数十分钟级、多窗会话小时级，
    # 缺省 7200；超时由看门狗终止该轮并凭恢复提示续跑）
    agent_round_timeout: float = 7200.0
    # [models].profile：模型档位（scripts/models.py 注册表，缺省 int8 定稿档）；
    # 下载入口 models.py 与 worker 启动（scripts/worker.py）读同一档位，组件级复用、切换预留
    models_profile: str = "int8"


def _int_of(table: dict, key: str, default: int, label: str) -> int:
    value = table.get(key, default)
    # TOML 的 true/false 是 bool，而 bool 是 int 的子类，须显式排除
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{label} 应为非负整数，实际 {value!r}。")
    return value


def _str_of(table: dict, key: str, default: str, label: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{label} 应为字符串，实际 {value!r}。")
    return value


def _api_section(data: dict, name: str, spec: tuple[tuple[str, str], ...]) -> dict:
    """解析 [llm]/[painter] API 段；spec = ((键, 默认值), ...)。"""
    section = data.get(name, {})
    if not isinstance(section, dict):
        raise ConfigError(f"[{name}] 应为表。")
    return {key: _str_of(section, key, default, f"{name}.{key}")
            for key, default in spec}


def load_config(root: Path) -> Config:
    """读取 root/config.toml；文件不存在时返回默认配置。"""
    path = root / "config.toml"
    if not path.is_file():
        return Config()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"无法读取 {path}：{exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} 不是合法 TOML：{exc}") from exc

    comfyui = data.get("comfyui", {})
    if not isinstance(comfyui, dict):
        raise ConfigError("[comfyui] 应为表。")
    mode = comfyui.get("mode", "bundled")
    if mode not in COMFYUI_MODES:
        raise ConfigError(f"comfyui.mode 须为 {'/'.join(COMFYUI_MODES)} 之一，实际 {mode!r}。")
    comfyui_path = str(comfyui.get("path", "") or "")
    if mode == "external" and not comfyui_path:
        raise ConfigError('comfyui.mode = "external" 时必须配置 comfyui.path。')

    paths = data.get("paths", {})
    if not isinstance(paths, dict):
        raise ConfigError("[paths] 应为表。")
    remake_home = paths.get("remake_home")
    if remake_home is not None and not isinstance(remake_home, str):
        raise ConfigError("paths.remake_home 应为字符串路径。")

    worker = data.get("worker", {})
    if not isinstance(worker, dict):
        raise ConfigError("[worker] 应为表。")
    gpu = _int_of(worker, "gpu", 0, "worker.gpu")
    max_480 = _int_of(worker, "max_480", 345, "worker.max_480")
    max_736 = _int_of(worker, "max_736", 175, "worker.max_736")
    comfy_flags = worker.get("comfy_flags", [])
    if not isinstance(comfy_flags, list) or not all(isinstance(item, str) for item in comfy_flags):
        raise ConfigError("worker.comfy_flags 应为字符串数组（如 [\"--vram-headroom\", \"6\"]）。")

    llm = _api_section(data, "llm", (
        ("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        ("api_key", ""),
        ("model", "qwen3.7-plus"),
    ))
    painter = _api_section(data, "painter", (
        ("base_url", "https://api.minimax.cn/v1"),
        ("api_key", ""),
        ("model", "MiniMax-H3-Image"),
    ))

    budget = data.get("budget", {})
    if not isinstance(budget, dict):
        raise ConfigError("[budget] 应为表。")
    max_images = budget.get("max_images", 24)
    if isinstance(max_images, bool) or not isinstance(max_images, int) or max_images < 1:
        raise ConfigError(f"budget.max_images 应为正整数，实际 {max_images!r}。")

    agent = data.get("agent", {})
    if not isinstance(agent, dict):
        raise ConfigError("[agent] 应为表。")
    round_timeout = agent.get("round_timeout", 7200.0)
    if isinstance(round_timeout, bool) or not isinstance(round_timeout, (int, float)) or round_timeout <= 0:
        raise ConfigError(f"agent.round_timeout 应为正数（秒），实际 {round_timeout!r}。")

    models = data.get("models", {})
    if not isinstance(models, dict):
        raise ConfigError("[models] 应为表。")
    models_profile = _str_of(models, "profile", "int8", "models.profile")
    if not models_profile.strip():
        raise ConfigError("models.profile 不得为空；可用档位见 scripts/models.py 注册表（当前 int8）。")

    return Config(
        comfyui_mode=mode,
        comfyui_path=comfyui_path,
        remake_home=remake_home or None,
        worker_gpu=gpu,
        worker_max_480=max_480,
        worker_max_736=max_736,
        worker_comfy_flags=tuple(comfy_flags),
        llm_model=llm["model"],
        llm_base_url=llm["base_url"],
        llm_api_key=llm["api_key"],
        painter_model=painter["model"],
        painter_base_url=painter["base_url"],
        painter_api_key=painter["api_key"],
        budget_max_images=max_images,
        agent_round_timeout=float(round_timeout),
        models_profile=models_profile,
    )


def resolve_remake_home(root: Path) -> Path:
    """REMAKE_HOME 生效路径：config.toml [paths].remake_home > 环境变量 > ~/.viggle-remake。"""
    configured = load_config(root).remake_home
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(os.environ.get("REMAKE_HOME") or Path.home() / ".viggle-remake").expanduser().resolve()
