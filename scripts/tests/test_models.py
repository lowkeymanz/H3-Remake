"""模型档位注册表 + 下载编排（scripts/models.py）+ 独立入口 models.py 的零费测试。

下载机制测试自原 test_installer.ModelStageTests 迁移（阶段 5 移出安装器）；
注册表/入口测试覆盖组件复用、档位切换预留与内存门槛。
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import models
from scripts.installer import Installer
from scripts.models import (
    COMPONENTS,
    PROFILES,
    ModelDownloader,
    ModelError,
    ModelProfile,
    ModelSpec,
    profile_specs,
    resolve_profile,
)


class ScriptedRunner:
    """记录命令并按需产生文件副作用的假 runner。"""

    def __init__(self, effects: dict[str, Callable[[list[str]], None]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.effects = effects or {}

    def __call__(self, command, *, cwd=None, env=None, check=True):
        normalized = [str(part) for part in command]
        self.calls.append(normalized)
        effect = self.effects.get(normalized[0])
        if effect:
            effect(normalized)
        return subprocess.CompletedProcess(normalized, 0)


def make_profile(*components: str) -> ModelProfile:
    return ModelProfile(name="test", description="测试档", components=components,
                        worker_script="remake_worker.py")


class ProfileRegistryTests(unittest.TestCase):
    def test_int8_profile_manifest_pinned(self) -> None:
        profile = resolve_profile("int8")
        specs = profile_specs(profile)
        self.assertEqual(len(specs), 5)
        # HF 实测字节清单锚定（与上游 int8 定稿档一致，漂移即报错提醒人工核对）
        self.assertEqual(sum(spec.size for spec in specs), 63_440_965_087)
        self.assertEqual(profile.worker_script, "remake_worker.py")
        self.assertEqual(profile.min_ram_gib, 64.0)
        self.assertEqual(PROFILES["int8"], profile)

    def test_unknown_profile_lists_available(self) -> None:
        with self.assertRaisesRegex(ModelError, "未知模型档位"):
            resolve_profile("gguf-q4")
        with self.assertRaisesRegex(ModelError, "int8"):
            resolve_profile("gguf-q4")

    def test_profile_specs_dedupes_shared_components(self) -> None:
        # 组件级复用：两个组件键指向同一 (repo, path) 时只下载一次；保序
        shared = ModelSpec("r/x", "vae/v.safetensors", 10, "vae")
        other = ModelSpec("r/x", "int8/a.safetensors", 20, "diffusion_models")
        with patch("scripts.models.COMPONENTS", {"k1": shared, "k2": shared, "k3": other}):
            specs = profile_specs(make_profile("k3", "k1", "k2"))
        self.assertEqual(specs, (other, shared))

    def test_components_registry_keys_cover_profiles(self) -> None:
        for profile in PROFILES.values():
            for key in profile.components:
                self.assertIn(key, COMPONENTS)


class DownloadTests(unittest.TestCase):
    def make_comfy(self, root: Path) -> Path:
        comfy = root / "vendor/ComfyUI"
        comfy.mkdir(parents=True)
        (comfy / "main.py").touch()
        return comfy

    def test_download_requires_comfyui_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            comfy = Path(temporary) / "vendor/ComfyUI"
            downloader = ModelDownloader(comfy, runner=ScriptedRunner(), log=lambda m: None)
            with self.assertRaisesRegex(ModelError, "install.py"):
                downloader.download(resolve_profile("int8"))

    def test_dry_run_validates_remote_without_download(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            comfy = self.make_comfy(Path(temporary))
            spec_missing = ModelSpec("r/x", "int8/a.safetensors", 100, "diffusion_models")
            spec_ready = ModelSpec("r/x", "text_encoders/b.safetensors", 50, "text_encoders")
            ready = comfy / "models/text_encoders/b.safetensors"
            ready.parent.mkdir(parents=True)
            ready.write_bytes(b"b" * 50)
            runner = ScriptedRunner()
            downloader = ModelDownloader(comfy, runner=runner, dry_run=True, log=lambda m: None)

            class Response:
                def __init__(self, status, headers):
                    self.status = status
                    self.headers = headers

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            def fake_urlopen(request, timeout=None):
                size = 100 if "a.safetensors" in request.full_url else 50
                return Response(206, {"Content-Range": f"bytes 0-0/{size}"})

            with patch("scripts.models.COMPONENTS", {"a": spec_missing, "b": spec_ready}):
                with patch("scripts.models.urllib.request.urlopen", fake_urlopen):
                    downloader.download(make_profile("a", "b"))
            self.assertEqual(runner.calls, [])
            self.assertFalse((comfy / "models/diffusion_models/a.safetensors").exists())

    def test_dry_run_reports_manifest_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            comfy = self.make_comfy(Path(temporary))
            spec = ModelSpec("r/x", "int8/a.safetensors", 100, "diffusion_models")
            downloader = ModelDownloader(comfy, runner=ScriptedRunner(), dry_run=True, log=lambda m: None)

            class Response:
                status = 206
                headers = {"Content-Range": "bytes 0-0/999"}

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            with patch("scripts.models.COMPONENTS", {"a": spec}):
                with patch("scripts.models.urllib.request.urlopen", lambda request, timeout=None: Response()):
                    with self.assertRaisesRegex(ModelError, "清单不符"):
                        downloader.download(make_profile("a"))

    def test_download_direct_and_staged_then_reentrant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            comfy = self.make_comfy(Path(temporary))
            spec_direct = ModelSpec("r/te", "text_encoders/b.safetensors", 50, "text_encoders")
            spec_int8 = ModelSpec("r/main", "int8/a.safetensors", 100, "diffusion_models")

            def fake_uvx(command):
                joined = " ".join(command)
                if "b.safetensors" in joined:
                    destination = comfy / "models/text_encoders/b.safetensors"
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(b"b" * 50)
                elif "a.safetensors" in joined:
                    staged = comfy / "models/.viggle-staging/int8/a.safetensors"
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    staged.write_bytes(b"a" * 100)

            runner = ScriptedRunner({"uvx": fake_uvx})
            downloader = ModelDownloader(comfy, runner=runner, log=lambda m: None)
            with patch("scripts.models.COMPONENTS", {"a": spec_direct, "b": spec_int8}):
                with patch.object(downloader, "remote_model_size", lambda spec: spec.size):
                    with patch("scripts.models.shutil.disk_usage",
                               return_value=types.SimpleNamespace(free=200 * 2**30)):
                        downloader.download(make_profile("a", "b"))
            self.assertEqual((comfy / "models/text_encoders/b.safetensors").stat().st_size, 50)
            self.assertEqual((comfy / "models/diffusion_models/a.safetensors").stat().st_size, 100)
            self.assertFalse((comfy / "models/.viggle-staging").exists())

            again = ScriptedRunner()
            downloader.runner = again
            with patch("scripts.models.COMPONENTS", {"a": spec_direct, "b": spec_int8}):
                downloader.download(make_profile("a", "b"))
            self.assertEqual(again.calls, [])

    def test_download_requires_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            comfy = self.make_comfy(Path(temporary))
            spec = ModelSpec("r/x", "int8/a.safetensors", 100, "diffusion_models")
            runner = ScriptedRunner()
            downloader = ModelDownloader(comfy, runner=runner, log=lambda m: None)
            with patch("scripts.models.COMPONENTS", {"a": spec}):
                with patch.object(downloader, "remote_model_size", lambda spec: spec.size):
                    with patch("scripts.models.shutil.disk_usage",
                               return_value=types.SimpleNamespace(free=0)):
                        with self.assertRaisesRegex(ModelError, "磁盘"):
                            downloader.download(make_profile("a"))
            self.assertEqual(runner.calls, [])


class ModelsEntryTests(unittest.TestCase):
    """根入口 models.py：档位解析（config/--profile）、内存门槛、dry-run 透传、错误口径。"""

    def make_root(self, tmp: str, *, profile_toml: str = "") -> tuple[Path, Installer]:
        root = Path(tmp)
        comfy = root / "vendor/ComfyUI"
        comfy.mkdir(parents=True)
        (comfy / "main.py").touch()
        if profile_toml:
            (root / "config.toml").write_text(f"[models]\n{profile_toml}", encoding="utf-8")
        return root, Installer(root, runner=ScriptedRunner())

    def test_list_profiles(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(models.main(["--list"]), 0)
        self.assertIn("int8", out.getvalue())

    def test_unknown_profile_from_config_fails_with_registry_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _root, installer = self.make_root(temporary, profile_toml='profile = "gguf-q4"\n')
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(models.main([], installer=installer), 1)
            self.assertIn("未知模型档位", errors.getvalue())
            self.assertIn("int8", errors.getvalue())

    def test_low_ram_fails_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _root, installer = self.make_root(temporary)
            errors = io.StringIO()
            with patch("models.total_ram_gib", return_value=32.0):
                with patch("scripts.models.ModelDownloader.download",
                           side_effect=AssertionError("内存不足时不得下载")):
                    with contextlib.redirect_stderr(errors):
                        self.assertEqual(models.main([], installer=installer), 1)
            self.assertIn("内存", errors.getvalue())
            self.assertIn("64 GiB", errors.getvalue())

    def test_download_receives_profile_and_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _root, installer = self.make_root(temporary)
            seen: list[tuple[str, bool]] = []

            def record(downloader_self, profile):
                seen.append((profile.name, downloader_self.dry_run))

            with patch("models.total_ram_gib", return_value=96.0):
                with patch("scripts.models.ModelDownloader.download", record):
                    self.assertEqual(models.main(["--dry-run"], installer=installer), 0)
            self.assertEqual(seen, [("int8", True)])

    def test_profile_flag_overrides_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _root, installer = self.make_root(temporary, profile_toml='profile = "gguf-q4"\n')
            seen: list[str] = []
            with patch("models.total_ram_gib", return_value=96.0):
                with patch("scripts.models.ModelDownloader.download",
                           lambda self, profile: seen.append(profile.name)):
                    self.assertEqual(models.main(["--profile", "int8"], installer=installer), 0)
            self.assertEqual(seen, ["int8"])

    def test_comfyui_missing_hints_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer = Installer(root, runner=ScriptedRunner())  # 未创建 ComfyUI
            errors = io.StringIO()
            with patch("models.total_ram_gib", return_value=96.0):
                with contextlib.redirect_stderr(errors):
                    self.assertEqual(models.main([], installer=installer), 1)
            self.assertIn("install.py", errors.getvalue())


if __name__ == "__main__":
    unittest.main()