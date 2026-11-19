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
from unittest.mock import patch

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
    venv_python,
)


class ScriptedRunner:
    """记录命令并按需产生文件副作用的假 runner。"""

    def __init__(self, effects: dict[str, Callable[[list[str]], None]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.cwds: list[Path | None] = []
        self.effects = effects or {}

    def __call__(self, command, *, cwd=None, env=None, check=True):
        normalized = [str(part) for part in command]
        self.calls.append(normalized)
        self.cwds.append(cwd)
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
                (comfy / "requirements.txt").touch()

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
            self.assertTrue(any("requirements.txt" in " ".join(call) for call in runner.calls))
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


if __name__ == "__main__":
    unittest.main()
