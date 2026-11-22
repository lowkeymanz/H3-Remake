from __future__ import annotations

import ast
import contextlib
import io
import os
import py_compile
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Callable
from unittest.mock import mock_open, patch

import install
from scripts.installer import (
    HERMES_COMMIT,
    COMFYUI_VERSION,
    DEV_STAGES,
    FULL_STAGES,
    InstallError,
    Installer,
    STAGE_NAMES,
    TORCH_INDEX_URL,
    total_ram_gib,
    venv_python,
)


class ScriptedRunner:
    """记录命令并按需产生文件副作用的假 runner。"""

    def __init__(self, effects: dict[str, Callable[[list[str]], None]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.cwds: list[Path | None] = []
        self.envs: list[dict[str, str] | None] = []
        self.effects = effects or {}

    def __call__(self, command, *, cwd=None, env=None, check=True):
        normalized = [str(part) for part in command]
        self.calls.append(normalized)
        self.cwds.append(cwd)
        self.envs.append(env)
        effect = self.effects.get(normalized[0])
        if effect:
            effect(normalized)
        return subprocess.CompletedProcess(normalized, 0)


# remake_worker.py 补丁形状Fixture：与官方源码同形的最小片段（resource 导入/峰值行/三处写入侧
# rename + 认领/回队 rename）
REMAKE_FIXTURE = (
    "import os\n"
    "import resource  # noqa: E402\n"
    "import socket\n\n"
    "def render(job):\n"
    "    tmp.rename(dst)\n\n"
    "def beat():\n"
    "    tmp.rename(beat_path)\n\n"
    "def loop(run):\n"
    '    info = {"peak_rss_gib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 1),\n'
    '            "host": host}\n'
    '    tmp = run.with_suffix(f".{os.getpid()}.tmp")\n'
    '    tmp.rename(run.with_suffix(".done"))\n'
    '    run.rename(run.with_suffix(".json"))\n'
    "    p.rename(run)\n"
)


def _peak_helper_source(text: str) -> str:
    """从补丁后源码 ast 提取 peak_rss_gib helper（不执行重导入链，零费验证双分支）。"""
    for node in ast.parse(text).body:
        if isinstance(node, ast.FunctionDef) and node.name == "peak_rss_gib":
            return ast.get_source_segment(text, node)
    raise AssertionError("补丁后源码未找到 peak_rss_gib helper")


class InstallerTests(unittest.TestCase):
    def test_venv_python_is_platform_specific(self) -> None:
        venv = Path("runtime")
        self.assertEqual(venv_python(venv, "win32"), Path("runtime/Scripts/python.exe"))
        self.assertEqual(venv_python(venv, "linux"), Path("runtime/bin/python"))

    def test_missing_prerequisite_is_reported_without_changing_system(self) -> None:
        installer = Installer(Path("unused"))
        with patch("scripts.installer.shutil.which", side_effect=lambda name: "/usr/bin/uv" if name == "uv" else None):
            with self.assertRaisesRegex(InstallError, "git"):
                installer.preflight()

    def test_compatibility_patches_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer = Installer(root)
            paint = installer.mcp_source / "viggle_remake_mcp/paint.py"
            paint.parent.mkdir(parents=True)
            paint.write_text(
                "def gpt(images, prompt, size, quality):\n"
                "    if images:\n"
                "        r = c.images.edit(model=MODEL, prompt=prompt, size=size, quality=quality)\n"
                "    else:\n"
                "        r = c.images.generate(model=MODEL, prompt=prompt, size=size, quality=quality)\n\n"
                "def next_function():\n"
                "    pass\n",
                encoding="utf-8",
            )
            worker = installer.mcp_source / "tests/fake_worker.py"
            worker.parent.mkdir(parents=True)
            worker.write_text(
                'tmp.rename(workers / f"fake_{a.name}.json")\n'
                'tmp.rename(job["out"])\n'
                'tmp.rename(run.with_suffix(".done"))\n',
                encoding="utf-8",
            )
            remake = installer.mcp_source / "comfy_worker/remake_worker.py"
            remake.parent.mkdir(parents=True)
            remake.write_text(REMAKE_FIXTURE, encoding="utf-8")

            installer.apply_compatibility_patches()
            first = (paint.read_text(encoding="utf-8"), worker.read_text(encoding="utf-8"),
                     remake.read_text(encoding="utf-8"))
            installer.apply_compatibility_patches()
            second = (paint.read_text(encoding="utf-8"), worker.read_text(encoding="utf-8"),
                      remake.read_text(encoding="utf-8"))

            self.assertEqual(first, second)
            self.assertEqual(first[0].count('response_format="b64_json"'), 2)
            self.assertEqual(first[1].count("tmp.replace("), 2)
            self.assertIn('tmp.rename(run.with_suffix(".done"))', first[1])
            self.assertEqual(first[2].count("tmp.replace("), 3)

    def test_complete_source_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in (
                "mcp/pyproject.toml",
                "mcp/viggle_remake_mcp/paint.py",
                "mcp/tests/fake_worker.py",
                "mcp/comfy_worker/remake_worker.py",
                "comfyui/ComfyUI-Viggle-Remake/__init__.py",
                "comfyui/ComfyUI-Viggle-Remake/nodes.py",
            ):
                path = root / "vendor/viggle-remake" / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            calls = []
            installer = Installer(root, runner=lambda *args, **kwargs: calls.append(args))
            installer.ensure_source()
            self.assertEqual(calls, [])

    def test_incomplete_source_is_repaired_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "vendor/viggle-remake"
            for relative in (
                "mcp/pyproject.toml",
                "mcp/viggle_remake_mcp/paint.py",
                "mcp/tests/fake_worker.py",
            ):
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()

            def fake_download(command):
                for relative in (
                    "mcp/comfy_worker/remake_worker.py",
                    "comfyui/ComfyUI-Viggle-Remake/__init__.py",
                    "comfyui/ComfyUI-Viggle-Remake/nodes.py",
                ):
                    path = source / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()

            runner = ScriptedRunner({"uvx": fake_download})
            installer = Installer(root, runner=runner)
            installer.ensure_source()
            self.assertEqual(len(runner.calls), 1)
            # 两个包含模式必须是两个独立的 --include（hf CLI 单个 --include 只吃一个模式）
            self.assertEqual(runner.calls[0].count("--include"), 2)
            self.assertTrue((source / "comfyui/ComfyUI-Viggle-Remake/nodes.py").is_file())

            installer.ensure_source()  # 补齐后不再下载
            self.assertEqual(len(runner.calls), 1)

    def test_hermes_marker_accepts_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer = Installer(Path(temporary), remake_home=Path(temporary) / "home")
            installer.hermes_source.mkdir(parents=True)
            (installer.hermes_source / ".hermes-bootstrap-complete").write_text(
                f'{{"pinnedCommit": "{HERMES_COMMIT}"}}', encoding="utf-8-sig"
            )
            self.assertIsNone(installer.verify_existing_hermes())

    def test_unexpected_exception_has_stage_and_nonzero_exit(self) -> None:
        class BrokenInstaller:
            current_stage = "阶段 3：MCP 独立环境"

            def run_dev(self) -> None:
                raise ValueError("injected failure")

        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            result = install.main(["--dev"], installer=BrokenInstaller())
        self.assertEqual(result, 1)
        self.assertIn("阶段 3：MCP 独立环境", errors.getvalue())
        self.assertIn("ValueError: injected failure", errors.getvalue())


class _EscapedInterpreterRunner:
    """底座解释器真实路径逸出运行树即失败的假 runner（模拟解释器自定位失效）。"""

    def __init__(self, runtime: Path) -> None:
        self.calls: list[list[str]] = []
        self._runtime = runtime

    def __call__(self, command, *, cwd=None, env=None, check=True):
        normalized = [str(part) for part in command]
        self.calls.append(normalized)
        if not Path(normalized[0]).resolve().is_relative_to(self._runtime):
            raise subprocess.CalledProcessError(1, normalized)
        return subprocess.CompletedProcess(normalized, 0)


class HermesRuntimeHealTests(unittest.TestCase):
    """Hermes 运行时自愈：底座解释器被运行树外符号链接顶替时还原为真实文件。"""

    def _symlink(self, link: Path, target: Path) -> None:
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("当前环境无法创建符号链接")

    def _broken_runtime(self, root: Path) -> tuple[Installer, Path, Path]:
        home = root / "home"
        base_bin = root / "vendor" / "hermes-runtime" / "python-3.14.9" / "bin"
        pool = root / "pool"
        base_bin.mkdir(parents=True)
        pool.mkdir()
        (pool / "python-binary").write_bytes(b"fake-interpreter")
        for name in ("python3", "python3.14"):
            self._symlink(base_bin / name, pool / "python-binary")
        venv = home / "hermes" / "installs" / "key" / "environments" / "gen" / "venv"
        (venv / "bin").mkdir(parents=True)
        self._symlink(venv / "bin" / "python", base_bin / "python3.14")
        (venv / "pyvenv.cfg").write_text(
            f"home = {base_bin.as_posix()}\n"
            "implementation = CPython\nversion_info = 3.14.9\nrelocatable = true\n",
            encoding="utf-8")
        runner = _EscapedInterpreterRunner((root / "vendor" / "hermes-runtime").resolve())
        installer = Installer(root, runner=runner, platform="linux", remake_home=home)
        return installer, venv, base_bin

    def test_heal_restores_escaped_base_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer, venv, base_bin = self._broken_runtime(root)
            python = venv_python(venv, "linux")
            self.assertFalse(installer.hermes_python_ok(python))
            healed = installer.heal_hermes_runtime()
            self.assertEqual(healed, python)
            materialized = base_bin / "python3.14"
            self.assertFalse(materialized.is_symlink())
            self.assertEqual(materialized.read_bytes(), b"fake-interpreter")
            recorded = Path((installer.hermes_home / "runtime-python.txt")
                            .read_text(encoding="utf-8").strip())
            self.assertEqual(recorded, python)
            self.assertTrue(installer.hermes_python_ok(python))

    def test_heal_keeps_in_tree_symlinks_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_bin = root / "vendor" / "hermes-runtime" / "python-3.14.9" / "bin"
            base_bin.mkdir(parents=True)
            (base_bin / "python3.14").write_bytes(b"real-interpreter")
            self._symlink(base_bin / "python3", base_bin / "python3.14")
            venv = root / "home" / "hermes" / "installs" / "key" / "environments" / "gen" / "venv"
            (venv / "bin").mkdir(parents=True)
            self._symlink(venv / "bin" / "python", base_bin / "python3.14")
            (venv / "pyvenv.cfg").write_text(
                f"home = {base_bin.as_posix()}\n"
                "implementation = CPython\nversion_info = 3.14.9\nrelocatable = true\n",
                encoding="utf-8")
            runner = _EscapedInterpreterRunner((root / "vendor" / "hermes-runtime").resolve())
            installer = Installer(root, runner=runner, platform="linux",
                                  remake_home=root / "home")
            self.assertEqual(installer.heal_hermes_runtime(), venv_python(venv, "linux"))
            self.assertTrue((base_bin / "python3").is_symlink())
            self.assertEqual((base_bin / "python3.14").read_bytes(), b"real-interpreter")

    def test_heal_returns_none_for_dangling_base(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            base_bin = root / "vendor" / "hermes-runtime" / "python-3.14.9" / "bin"
            base_bin.mkdir(parents=True)
            self._symlink(base_bin / "python3.14", root / "pool" / "missing")
            venv = home / "hermes" / "installs" / "key" / "environments" / "gen" / "venv"
            (venv / "bin").mkdir(parents=True)
            self._symlink(venv / "bin" / "python", base_bin / "python3.14")
            (venv / "pyvenv.cfg").write_text(
                f"home = {base_bin.as_posix()}\n"
                "implementation = CPython\nversion_info = 3.14.9\nrelocatable = true\n",
                encoding="utf-8")
            installer = Installer(root, runner=_EscapedInterpreterRunner(
                (root / "vendor" / "hermes-runtime").resolve()),
                platform="linux", remake_home=home)
            self.assertIsNone(installer.heal_hermes_runtime())

    def test_ensure_returns_none_when_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer = Installer(Path(temporary), remake_home=Path(temporary) / "home")
            self.assertIsNone(installer.ensure_hermes_runtime())

    def test_ensure_heals_installed_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer, venv, _ = self._broken_runtime(root)
            installer.hermes_source.mkdir(parents=True)
            (installer.hermes_source / ".hermes-bootstrap-complete").write_text(
                f'{{"pinnedCommit": "{HERMES_COMMIT}"}}', encoding="utf-8")
            self.assertEqual(installer.ensure_hermes_runtime(), venv_python(venv, "linux"))
            self.assertFalse(any(call[0] in ("bash", "powershell", "pwsh")
                                 for call in installer.runner.calls))


class RemakeWorkerPatchTests(unittest.TestCase):
    """remake_worker.py Windows 兼容补丁（成品双平台口径）：幂等/形状校验/helper 双分支/真源码编译。"""

    def _worker(self, root: Path, text: str = REMAKE_FIXTURE) -> tuple[Installer, Path]:
        installer = Installer(root)
        worker = installer.mcp_source / "comfy_worker/remake_worker.py"
        worker.parent.mkdir(parents=True, exist_ok=True)
        worker.write_text(text, encoding="utf-8")
        return installer, worker

    def test_patch_rewrites_resource_and_write_renames_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer, worker = self._worker(Path(temporary))
            installer.patch_remake_worker()
            first = worker.read_text(encoding="utf-8")
            self.assertIn("except ImportError:  # Windows: resource is Unix-only", first)
            self.assertIn('"peak_rss_gib": peak_rss_gib(),', first)
            self.assertEqual(first.count("tmp.replace("), 3)  # 心跳/.done/成片
            self.assertIn('run.rename(run.with_suffix(".json"))', first)  # 回队保留 rename
            self.assertIn("p.rename(run)", first)  # 原子认领保留 rename
            py_compile.compile(str(worker), doraise=True)
            installer.patch_remake_worker()
            self.assertEqual(worker.read_text(encoding="utf-8"), first)  # 幂等

    def test_resource_shape_drift_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer, _worker = self._worker(
                Path(temporary), REMAKE_FIXTURE.replace("import resource  # noqa: E402", "import resource"))
            with self.assertRaisesRegex(InstallError, "resource 形式"):
                installer.patch_remake_worker()

    def test_write_shape_drift_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer, _worker = self._worker(
                Path(temporary), REMAKE_FIXTURE.replace("    tmp.rename(beat_path)\n", ""))
            with self.assertRaisesRegex(InstallError, "写入形式"):
                installer.patch_remake_worker()

    def test_peak_helper_unix_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer, worker = self._worker(Path(temporary))
            installer.patch_remake_worker()
            stub = types.SimpleNamespace(
                RUSAGE_SELF=0, getrusage=lambda _who: types.SimpleNamespace(ru_maxrss=2 * 2**20))
            namespace: dict = {"resource": stub}
            exec(_peak_helper_source(worker.read_text(encoding="utf-8")), namespace)
            self.assertEqual(namespace["peak_rss_gib"](), 2.0)  # KiB → GiB

    @unittest.skipUnless(sys.platform == "win32", "Windows 分支需真实 GetProcessMemoryInfo")
    def test_peak_helper_windows_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer, worker = self._worker(Path(temporary))
            installer.patch_remake_worker()
            namespace: dict = {"resource": None}  # 模拟 win 上 resource 导入失败
            exec(_peak_helper_source(worker.read_text(encoding="utf-8")), namespace)
            value = namespace["peak_rss_gib"]()
            self.assertIsInstance(value, float)
            self.assertGreaterEqual(value, 0.0)

    def test_real_vendor_worker_patched_compiles(self) -> None:
        real = Path(__file__).resolve().parents[2] / "vendor/viggle-remake/mcp/comfy_worker/remake_worker.py"
        if not real.is_file():
            self.skipTest("vendor 源码未安装（阶段 2 后可验）")
        with tempfile.TemporaryDirectory() as temporary:
            installer, worker = self._worker(Path(temporary), real.read_text(encoding="utf-8"))
            installer.patch_remake_worker()
            patched = worker.read_text(encoding="utf-8")
            self.assertIn("except ImportError:  # Windows: resource is Unix-only", patched)
            self.assertEqual(patched.count("tmp.replace("), 3)
            py_compile.compile(str(worker), doraise=True)  # 真实形状补丁后语法有效
            installer.patch_remake_worker()
            self.assertEqual(worker.read_text(encoding="utf-8"), patched)


class ComfyUiStageTests(unittest.TestCase):
    def test_resolve_comfyui_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer = Installer(root, runner=ScriptedRunner())
            self.assertEqual(installer.resolve_comfyui(), (installer.vendor / "ComfyUI", "bundled"))

            ext = root / "ext"
            ext.mkdir()
            (ext / "main.py").touch()
            (root / "config.toml").write_text(
                f'[comfyui]\nmode = "external"\npath = "{ext.as_posix()}"\n', encoding="utf-8")
            installer = Installer(root, runner=ScriptedRunner())
            self.assertEqual(installer.resolve_comfyui(), (ext.resolve(), "external"))

            (root / "config.toml").write_text(
                '[comfyui]\nmode = "external"\npath = "nowhere"\n', encoding="utf-8")
            installer = Installer(root, runner=ScriptedRunner())
            with self.assertRaisesRegex(InstallError, "main.py"):
                installer.resolve_comfyui()

            (root / "config.toml").write_text('[comfyui]\nmode = "auto"\n', encoding="utf-8")
            installer = Installer(root, runner=ScriptedRunner())
            with patch.object(Installer, "comfyui_candidates", return_value=[]):
                self.assertEqual(installer.resolve_comfyui(), (installer.vendor / "ComfyUI", "bundled"))

            candidate = root / "found"
            candidate.mkdir()
            (candidate / "main.py").touch()
            (candidate / "comfyui_version.py").write_text(
                f'__version__ = "{COMFYUI_VERSION}"', encoding="utf-8")
            with patch.object(installer, "comfyui_candidates", return_value=[candidate]):
                with patch("builtins.input", return_value="y"):
                    self.assertEqual(installer.resolve_comfyui(), (candidate.resolve(), "external"))

    def test_bundled_comfyui_install_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            comfy = root / "vendor/ComfyUI"

            def fake_clone(command):
                comfy.mkdir(parents=True, exist_ok=True)
                (comfy / "comfyui_version.py").write_text(
                    f'__version__ = "{COMFYUI_VERSION}"', encoding="utf-8")
                (comfy / "main.py").touch()
                (comfy / "requirements.txt").write_text(
                    "comfyui-frontend-package==1.55.18\n"
                    "comfyui-workflow-templates==0.11.89\n"
                    "torch\n", encoding="utf-8")

            def fake_uv(command):
                if command[1] == "venv":
                    python = venv_python(comfy / "venv", "win32")
                    python.parent.mkdir(parents=True, exist_ok=True)
                    python.touch()

            runner = ScriptedRunner({"git": fake_clone, "uv": fake_uv})
            installer = Installer(root, runner=runner, platform="win32")
            installer.install_comfyui()

            clone = runner.calls[0]
            self.assertEqual(clone[:2], ["git", "clone"])
            self.assertEqual(clone[clone.index("--branch") + 1], "v0.42.0")
            self.assertEqual(clone[-1], str(comfy))
            torch_call = next(call for call in runner.calls if "torch==2.9.1" in call)
            self.assertEqual(torch_call[torch_call.index("--index-url") + 1], TORCH_INDEX_URL)
            for pin in ("torchvision==0.24.1", "torchaudio==2.9.1"):
                self.assertIn(pin, torch_call)
            self.assertTrue(any("requirements.viggle.txt" in " ".join(call) for call in runner.calls))
            # 模板包仅供 Web UI 画廊（软依赖）且 media-assets 已从 PyPI 消失：过滤后安装，上游原文件不动
            filtered = (comfy / "requirements.viggle.txt").read_text(encoding="utf-8")
            self.assertNotIn("comfyui-workflow-templates", filtered)
            self.assertIn("comfyui-frontend-package==1.55.18", filtered)
            self.assertIn("comfyui-workflow-templates==0.11.89",
                          (comfy / "requirements.txt").read_text(encoding="utf-8"))
            verify = [call for call in runner.calls if len(call) == 3 and call[1] == "-c"]
            self.assertEqual(len(verify), 1)
            self.assertIn("torch", verify[0][2])

            second = ScriptedRunner()
            installer.runner = second
            installer.install_comfyui()
            self.assertEqual([call for call in second.calls if call[0] == "git"], [])
            self.assertEqual([call for call in second.calls if call[:2] == ["uv", "venv"]], [])
            self.assertEqual(len([call for call in second.calls if call[:2] == ["uv", "pip"]]), 2)

    def test_bundled_comfyui_version_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            comfy = root / "vendor/ComfyUI"
            comfy.mkdir(parents=True)
            (comfy / "main.py").touch()
            (comfy / "comfyui_version.py").write_text('__version__ = "0.41.0"', encoding="utf-8")
            installer = Installer(root, runner=ScriptedRunner(), platform="win32")
            with self.assertRaisesRegex(InstallError, COMFYUI_VERSION):
                installer.install_comfyui()

    def test_filtered_requirements_drops_templates_only(self) -> None:
        # comfyui-workflow-templates 锁定的 media-assets 依赖已从 PyPI 消失，
        # 原样安装必然解析失败；该包仅供 Web UI 模板画廊（软依赖），worker 库导入渲染不需要
        with tempfile.TemporaryDirectory() as temporary:
            comfy = Path(temporary)
            original = (
                "comfyui-frontend-package==1.55.18\n"
                "comfyui-workflow-templates==0.11.89\n"
                "comfyui-workflow-templates-media-assets-02==0.1.17\n"
                "torch\n"
                "#non essential dependencies:\n"
                "kornia>=0.7.1\n"
            )
            (comfy / "requirements.txt").write_text(original, encoding="utf-8")
            installer = Installer(Path("unused"), runner=ScriptedRunner())
            filtered = installer.filtered_requirements(comfy)
            self.assertEqual(filtered, comfy / "requirements.viggle.txt")
            content = filtered.read_text(encoding="utf-8")
            self.assertNotIn("workflow-templates", content)
            self.assertIn("comfyui-frontend-package==1.55.18\n", content)
            self.assertIn("torch\n", content)
            self.assertIn("#non essential dependencies:\n", content)
            self.assertIn("kornia>=0.7.1\n", content)
            self.assertEqual((comfy / "requirements.txt").read_text(encoding="utf-8"), original)
            stamp = filtered.stat().st_mtime_ns
            installer.filtered_requirements(comfy)  # 重入：内容不变不重写
            self.assertEqual(filtered.stat().st_mtime_ns, stamp)

    def test_external_comfyui_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ext = root / "ext"
            ext.mkdir()
            (ext / "main.py").touch()
            (ext / "comfyui_version.py").write_text(
                f'__version__ = "{COMFYUI_VERSION}"', encoding="utf-8")
            (root / "config.toml").write_text(
                f'[comfyui]\nmode = "external"\npath = "{ext.as_posix()}"\n', encoding="utf-8")

            installer = Installer(root, runner=ScriptedRunner(), platform="win32")
            with self.assertRaisesRegex(InstallError, "bundled"):
                installer.install_comfyui()

            python = venv_python(ext / "venv", "win32")
            python.parent.mkdir(parents=True)
            python.touch()
            runner = ScriptedRunner()
            installer = Installer(root, runner=runner, platform="win32")
            installer.install_comfyui()
            verify = [call for call in runner.calls if len(call) == 3 and call[1] == "-c"]
            self.assertEqual(len(verify), 1)
            self.assertEqual(verify[0][0], str(python))

    def test_full_preflight_checks_gpu_and_disk(self) -> None:
        installer = Installer(Path("unused"))
        with patch("scripts.installer.shutil.which",
                   side_effect=lambda name: {"uv": "uv", "git": "git", "npm": "npm"}.get(name)):
            with self.assertRaisesRegex(InstallError, "nvidia-smi"):
                installer.preflight(full=True)

        def which_all(name):
            return {"uv": "uv", "git": "git", "npm": "npm", "nvidia-smi": "nvidia-smi"}.get(name)

        # 环境安装磁盘门槛 20 GiB（模型磁盘/内存检查已移至 models.py 入口，见 test_models）
        with patch("scripts.installer.shutil.which", side_effect=which_all):
            with patch("scripts.installer.shutil.disk_usage",
                       return_value=types.SimpleNamespace(free=10 * 2**30)):
                with self.assertRaisesRegex(InstallError, "磁盘"):
                    installer.preflight(full=True)
            with patch("scripts.installer.shutil.disk_usage",
                       return_value=types.SimpleNamespace(free=200 * 2**30)):
                with patch.object(installer, "configure_ffmpeg", return_value=Path("ffmpeg")):
                    installer.preflight(full=True)


class RamProbeTests(unittest.TestCase):
    """Linux 内存探测：sysconf 常量名须为 SC_*（误写 SCM_* 致内存恒 0），不可用时回落 /proc/meminfo。"""

    def test_posix_sysconf_uses_sc_names(self) -> None:
        asked: list[str] = []

        def fake_sysconf(name: str) -> int:
            asked.append(name)
            return {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 100 * 2**30 // 4096}[name]

        with patch("scripts.installer.sys.platform", "linux"):
            with patch("scripts.installer.os.sysconf", side_effect=fake_sysconf, create=True):
                self.assertEqual(total_ram_gib(), 100.0)
        self.assertEqual(asked, ["SC_PAGE_SIZE", "SC_PHYS_PAGES"])  # 防 SCM_* 拼写回归

    def test_posix_falls_back_to_meminfo(self) -> None:
        meminfo = "MemFree:        1234567 kB\nMemTotal:       100663296 kB\n"
        with patch("scripts.installer.sys.platform", "linux"):
            with patch("scripts.installer.os.sysconf", side_effect=ValueError("unavailable"), create=True):
                with patch("builtins.open", mock_open(read_data=meminfo)):
                    self.assertEqual(total_ram_gib(), 96.0)

    def test_posix_both_probes_fail_returns_zero(self) -> None:
        with patch("scripts.installer.sys.platform", "linux"):
            with patch("scripts.installer.os.sysconf", side_effect=OSError("no sysconf"), create=True):
                with patch("builtins.open", side_effect=OSError("no meminfo")):
                    self.assertEqual(total_ram_gib(), 0.0)


class NodeStageTests(unittest.TestCase):
    def test_dir_link_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer = Installer(root)
            target = root / "target"
            target.mkdir()
            (target / "main.py").touch()
            link = root / "link"

            installer.ensure_dir_link(target, link, label="测试")
            self.assertEqual(installer._dir_link_target(link), target.resolve())
            self.assertTrue((link / "main.py").is_file())

            installer.ensure_dir_link(target, link, label="测试")  # 幂等

            other = root / "other"
            other.mkdir()
            installer.ensure_dir_link(other, link, label="测试")  # 失效/换目标 → 重建
            self.assertEqual(installer._dir_link_target(link), other.resolve())
            installer.ensure_dir_link(target, link, label="测试")
            self.assertEqual(installer._dir_link_target(link), target.resolve())

            foreign = root / "foreign"
            foreign.mkdir()
            with self.assertRaisesRegex(InstallError, "非链接"):
                installer.ensure_dir_link(target, foreign, label="测试")

    def test_nodes_and_worker_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installer = Installer(root)

            comfy = installer.vendor / "ComfyUI"
            comfy.mkdir(parents=True)
            (comfy / "main.py").touch()
            with self.assertRaisesRegex(InstallError, "阶段 2"):
                installer.install_nodes_and_worker()

            nodes = installer.source / "comfyui/ComfyUI-Viggle-Remake"
            nodes.mkdir(parents=True)
            (nodes / "__init__.py").touch()
            (nodes / "nodes.py").touch()
            worker_dir = installer.mcp_source / "comfy_worker"
            worker_dir.mkdir(parents=True)

            installer.install_nodes_and_worker()
            self.assertTrue((comfy / "custom_nodes/ComfyUI-Viggle-Remake/nodes.py").is_file())
            self.assertTrue((worker_dir / "ComfyUI/main.py").is_file())
            installer.install_nodes_and_worker()  # 重入幂等

            fresh = Installer(Path(temporary) / "fresh")
            with self.assertRaisesRegex(InstallError, "阶段 4"):
                fresh.install_nodes_and_worker()


class WebBuildTests(unittest.TestCase):
    """阶段 9：npm 前端构建（可重入 + 新鲜度跳过 + 形状校验，零网络零费）。"""

    def make_web(self, root: Path, *, lockfile: bool = True) -> Path:
        web = root / "app" / "web"
        (web / "src").mkdir(parents=True)
        (web / "package.json").write_text('{"scripts": {"build": "vite build"}}', encoding="utf-8")
        (web / "src" / "main.tsx").write_text("// app\n", encoding="utf-8")
        if lockfile:
            (web / "package-lock.json").write_text("{}", encoding="utf-8")
        return web

    def build_effect(self, web: Path) -> Callable[[list[str]], None]:
        def effect(command: list[str]) -> None:
            if command[1:] == ["run", "build"]:
                dist = web / "dist"
                dist.mkdir(parents=True, exist_ok=True)
                (dist / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
        return effect

    def test_stage1_wires_sync_and_web_build(self) -> None:
        # 前端构建并入阶段 1（项目环境 = Python 依赖 + 前端产物），不新增阶段编号：
        # 需求第五章阶段 9 恒指 start.py 服务启动，安装器阶段保持 0–8 对齐
        self.assertNotIn("9", STAGE_NAMES)
        self.assertEqual(FULL_STAGES[-1], "8")
        self.assertIn("1", DEV_STAGES)
        installer = Installer(Path("unused"), runner=ScriptedRunner())
        executed: list[str] = []
        with patch.object(installer, "sync_project", lambda: executed.append("sync")):
            with patch.object(installer, "build_web", lambda: executed.append("web")):
                installer.run_stages(["1"])
        self.assertEqual(executed, ["sync", "web"])

    def test_build_runs_npm_ci_then_build_in_web_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            web = self.make_web(root)
            runner = ScriptedRunner({"npm": self.build_effect(web)})
            installer = Installer(root, runner=runner)
            with patch("scripts.installer.shutil.which", lambda name: "npm" if name == "npm" else None):
                installer.build_web()
            self.assertEqual(runner.calls, [["npm", "ci"], ["npm", "run", "build"]])
            self.assertEqual(runner.cwds, [web, web])
            self.assertTrue((web / "dist" / "index.html").is_file())

    def test_reentrant_skips_when_dist_current_and_rebuilds_on_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            web = self.make_web(root)
            runner = ScriptedRunner({"npm": self.build_effect(web)})
            installer = Installer(root, runner=runner)
            with patch("scripts.installer.shutil.which", lambda name: "npm" if name == "npm" else None):
                installer.build_web()
                installer.build_web()  # dist 新于全部输入 → 跳过
                self.assertEqual(len(runner.calls), 2)
                # 源码变更（mtime 拨到未来，避开文件系统时间粒度）→ 重新构建
                source = web / "src" / "main.tsx"
                future = source.stat().st_mtime + 10
                os.utime(source, (future, future))
                installer.build_web()
                self.assertEqual(len(runner.calls), 4)

    def test_no_lockfile_falls_back_to_npm_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            web = self.make_web(root, lockfile=False)
            runner = ScriptedRunner({"npm": self.build_effect(web)})
            installer = Installer(root, runner=runner)
            with patch("scripts.installer.shutil.which", lambda name: "npm" if name == "npm" else None):
                installer.build_web()
            self.assertEqual(runner.calls[0], ["npm", "install"])

    def test_missing_npm_reports_node_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_web(root)
            runner = ScriptedRunner()
            installer = Installer(root, runner=runner)
            with patch("scripts.installer.shutil.which", lambda name: None):
                with self.assertRaisesRegex(InstallError, "Node.js"):
                    installer.build_web()
            self.assertEqual(runner.calls, [])

    def test_missing_dist_after_build_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_web(root)
            installer = Installer(root, runner=ScriptedRunner())  # 无副作用：build 不产出 dist
            with patch("scripts.installer.shutil.which", lambda name: "npm" if name == "npm" else None):
                with self.assertRaisesRegex(InstallError, "dist/index.html"):
                    installer.build_web()

    def test_missing_frontend_source_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer = Installer(Path(temporary), runner=ScriptedRunner())
            with self.assertRaisesRegex(InstallError, "前端源码缺失"):
                installer.build_web()

    def test_dry_run_outputs_plan_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_web(root)
            runner = ScriptedRunner()
            installer = Installer(root, runner=runner, dry_run=True)
            with patch("scripts.installer.shutil.which", lambda name: "npm" if name == "npm" else None):
                installer.build_web()
            self.assertEqual(runner.calls, [])


class StageCliTests(unittest.TestCase):
    def test_run_stages_validates_and_orders(self) -> None:
        installer = Installer(Path("unused"), runner=ScriptedRunner())
        with self.assertRaisesRegex(InstallError, "未知阶段"):
            installer.run_stages(["9"])
        executed: list[str] = []
        with patch.object(installer, "sync_project", lambda: executed.append("1")):
            with patch.object(installer, "build_web", lambda: None):
                with patch.object(installer, "install_comfyui", lambda: executed.append("4")):
                    installer.run_stages(["4", "1"])
        self.assertEqual(executed, ["1", "4"])

    def test_stage5_moved_to_models_entry(self) -> None:
        # 阶段 5 编号保留但逻辑已移至独立入口 models.py；其余阶段编号不回补
        self.assertNotIn("5", STAGE_NAMES)
        self.assertEqual(FULL_STAGES, ("0", "1", "2", "3", "4", "6", "7", "8"))
        installer = Installer(Path("unused"), runner=ScriptedRunner())
        with self.assertRaisesRegex(InstallError, "models.py"):
            installer.run_stages(["5"])
        with self.assertRaisesRegex(InstallError, "models.py"):
            installer.run_stages(["4", "5", "6"])

    def test_cli_dispatch_and_conflicts(self) -> None:
        class Recording:
            current_stage = "完成"

            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def run_stages(self, ids):
                self.calls.append(("stages", list(ids)))

            def run_dev(self) -> None:
                self.calls.append(("dev",))

            def run_full(self) -> None:
                self.calls.append(("full",))

        recording = Recording()
        self.assertEqual(install.main(["--stages", "6,4"], installer=recording), 0)
        # 传参为原始解析结果；排序由真实 run_stages 完成（见上一测试）
        self.assertEqual(recording.calls, [("stages", ["6", "4"])])

        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit):
                install.main(["--dev", "--stages", "4"], installer=recording)

        fresh = Recording()
        self.assertEqual(install.main([], installer=fresh), 0)
        self.assertEqual(fresh.calls, [("full",)])


def _fail(command: list[str]) -> None:
    raise subprocess.CalledProcessError(1, command)


class HermesQuarantineRebuildTests(unittest.TestCase):
    """复验失败时：隔离坏 venv + 清 stale 记录 → 官方安装器重建 → 复验 → 清理隔离。"""

    def _installer(self, root: Path, runner) -> Installer:
        return Installer(root, remake_home=root / "home", platform="linux", runner=runner)

    def _venv(self, installer: Installer, install_id: str, env_id: str) -> Path:
        venv = installer.hermes_home / f"installs/{install_id}/environments/{env_id}/venv"
        python = venv / "bin/python"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"fake interpreter")
        return python

    def _marker(self, installer: Installer) -> None:
        installer.hermes_source.mkdir(parents=True, exist_ok=True)
        (installer.hermes_source / ".hermes-bootstrap-complete").write_text(
            f'{{"pinnedCommit": "{HERMES_COMMIT}"}}', encoding="utf-8")
        (installer.hermes_source / "scripts").mkdir(parents=True, exist_ok=True)
        (installer.hermes_source / "scripts/install.sh").touch()

    def test_quarantine_moves_broken_and_clears_stale_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer = self._installer(Path(temporary), ScriptedRunner())
            good = self._venv(installer, "aaaa", "good")
            bad = self._venv(installer, "bbbb", "bad")
            installer.runner = ScriptedRunner({str(bad): _fail})
            (installer.hermes_home / "runtime-python.txt").write_text(f"{bad}\n", encoding="utf-8")

            quarantined = installer.quarantine_broken_hermes_envs()
            self.assertEqual(quarantined, [bad.parent.parent.with_name("venv.broken")])
            self.assertTrue(good.is_file())
            self.assertFalse(bad.exists())
            self.assertFalse((installer.hermes_home / "runtime-python.txt").exists())
            self.assertEqual(installer.quarantine_broken_hermes_envs(), [])  # 重入无副作用

    def test_install_hermes_rebuilds_after_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = ScriptedRunner()
            installer = self._installer(Path(temporary), runner)
            self._marker(installer)
            bad = self._venv(installer, "aaaa", "bad")
            fresh = installer.hermes_home / "installs/cccc/environments/dddd/venv/bin/python"

            def rebuild(command: list[str]) -> None:
                fresh.parent.mkdir(parents=True, exist_ok=True)
                fresh.write_bytes(b"fake interpreter")

            installer.runner = ScriptedRunner({str(bad): _fail, "bash": rebuild})
            with patch.object(installer, "configure_ffmpeg", return_value=Path("ffmpeg")):
                installer.install_hermes()

            bash_calls = [call for call in installer.runner.calls if call[0] == "bash"]
            self.assertEqual(len(bash_calls), 1)
            self.assertEqual(bash_calls[0][bash_calls[0].index("--commit") + 1], HERMES_COMMIT)
            self.assertFalse(bad.parent.parent.exists())
            self.assertFalse(bad.parent.parent.with_name("venv.broken").exists())  # 成功后清理隔离
            self.assertEqual((installer.hermes_home / "runtime-python.txt").read_text(encoding="utf-8"),
                             f"{fresh}\n")

    def test_official_install_pins_runtime_to_persistent_tree(self) -> None:
        """官方安装器 env 须把运行时存储钉到项目持久树，防容器重启/迁移清掉 3.14 底座。"""
        with tempfile.TemporaryDirectory() as temporary:
            installer = self._installer(Path(temporary), ScriptedRunner())
            self._marker(installer)
            bad = self._venv(installer, "aaaa", "bad")
            fresh = installer.hermes_home / "installs/cccc/environments/dddd/venv/bin/python"

            def rebuild(command: list[str]) -> None:
                fresh.parent.mkdir(parents=True, exist_ok=True)
                fresh.write_bytes(b"fake interpreter")

            installer.runner = ScriptedRunner({str(bad): _fail, "bash": rebuild})
            with patch.object(installer, "configure_ffmpeg", return_value=Path("ffmpeg")):
                installer.install_hermes()

            bash_index = next(i for i, call in enumerate(installer.runner.calls) if call[0] == "bash")
            env = installer.runner.envs[bash_index]
            runtime_dir = installer.vendor / "hermes-runtime"
            self.assertEqual(env["HERMES_RUNTIME_DIR"], str(runtime_dir))  # PM store_root() 认此
            self.assertEqual(env["UV_PYTHON_INSTALL_DIR"], str(runtime_dir))  # bootstrap uv python install 认此
            self.assertEqual(env["UV_CACHE_DIR"], str(runtime_dir / "uv-cache"))  # 缓存不回落临时层
            self.assertEqual(env["UV_LINK_MODE"], "copy")  # 禁 hardlink 引用临时层文件
            self.assertEqual(env["HERMES_HOME"], str(installer.hermes_home))
            self.assertTrue(runtime_dir.is_relative_to(installer.vendor))  # 落持久盘而非临时层

    def test_failed_rebuild_keeps_quarantine_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer = self._installer(Path(temporary), ScriptedRunner())
            self._marker(installer)
            bad = self._venv(installer, "aaaa", "bad")
            installer.runner = ScriptedRunner({str(bad): _fail})  # bash 无副作用：重建未落位
            with patch.object(installer, "configure_ffmpeg", return_value=Path("ffmpeg")):
                with self.assertRaisesRegex(InstallError, "hermes_cli.main"):
                    installer.install_hermes()
            self.assertTrue(bad.parent.parent.with_name("venv.broken").exists())  # 留现场供诊断

    def _managed_python(self, installer: Installer, name: str) -> Path:
        entry = installer.vendor / "hermes-runtime" / name
        python = entry / "bin/python3"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"fake interpreter")
        return python

    def test_quarantine_broken_managed_python_keeps_healthy(self) -> None:
        """启动失败的托管 Python 挪边，健康的不动；bootstrap 只查可执行位，悬空底座会被沿用。"""
        with tempfile.TemporaryDirectory() as temporary:
            installer = self._installer(Path(temporary), ScriptedRunner())
            good = self._managed_python(installer, "python-3.14.9+20261021-linux-x64")
            bad = self._managed_python(installer, "python-3.14.9+20260101-linux-x64")
            installer.runner = ScriptedRunner({str(bad): _fail})

            quarantined = installer.quarantine_broken_managed_pythons()
            self.assertEqual(quarantined, [bad.parent.parent.with_name(bad.parent.parent.name + ".broken")])
            self.assertTrue(good.is_file())
            self.assertFalse(bad.exists())
            self.assertEqual(installer.quarantine_broken_managed_pythons(), [])  # 重入无副作用

    def test_rebuild_quarantines_broken_base_python_and_cleans_after_success(self) -> None:
        """重建前先隔离坏底座，否则 bootstrap 沿用悬空解释器，新 venv 依旧建在坏底座上。"""
        with tempfile.TemporaryDirectory() as temporary:
            installer = self._installer(Path(temporary), ScriptedRunner())
            self._marker(installer)
            bad_venv = self._venv(installer, "aaaa", "bad")
            bad_base = self._managed_python(installer, "python-3.14.9+20261021-linux-x64")
            fresh = installer.hermes_home / "installs/cccc/environments/dddd/venv/bin/python"

            def rebuild(command: list[str]) -> None:
                fresh.parent.mkdir(parents=True, exist_ok=True)
                fresh.write_bytes(b"fake interpreter")

            installer.runner = ScriptedRunner({str(bad_venv): _fail, str(bad_base): _fail, "bash": rebuild})
            with patch.object(installer, "configure_ffmpeg", return_value=Path("ffmpeg")):
                installer.install_hermes()

            broken_base = bad_base.parent.parent.with_name(bad_base.parent.parent.name + ".broken")
            self.assertFalse(broken_base.exists())  # 重建成功后清理隔离
            self.assertEqual((installer.hermes_home / "runtime-python.txt").read_text(encoding="utf-8"),
                             f"{fresh}\n")


class FfmpegResolutionTests(unittest.TestCase):
    def test_ffmpeg_found_in_persistent_runtime_store(self) -> None:
        """PATH 无 ffmpeg 时，须能从钉到持久树的运行时存储（PM ffmpeg 新落点）解析。"""
        with tempfile.TemporaryDirectory() as temporary:
            installer = Installer(Path(temporary), remake_home=Path(temporary) / "home", platform="linux")
            bin_dir = installer.vendor / "hermes-runtime/ffmpeg-9.0.1-linux-x64/bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "ffmpeg").touch()
            version = subprocess.CompletedProcess([], 0, stdout="ffmpeg version n9.0.1 Copyright")
            original_path = os.environ["PATH"]
            try:
                with patch("scripts.installer.shutil.which", return_value=None), \
                        patch("scripts.installer.subprocess.run", return_value=version):
                    resolved = installer.configure_ffmpeg()
                self.assertEqual(resolved, bin_dir)
                self.assertTrue(os.environ["PATH"].startswith(str(bin_dir)))
            finally:
                os.environ["PATH"] = original_path


if __name__ == "__main__":
    unittest.main()
