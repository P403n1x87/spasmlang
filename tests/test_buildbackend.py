"""End-to-end tests for the wheel-compiling build backend wrapper."""

import importlib
import sys
import zipfile
from pathlib import Path

import pytest

from spasm import buildbackend

PY = sys.version_info[:2]
RESUME = "resume 0\n" if PY >= (3, 11) else ""

_PYPROJECT = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "fixture-pkg"
version = "0.1.0"

[tool.hatch.build.targets.wheel]
packages = ["pkg"]
"""

_ASM_SOURCE = f"""\
{RESUME}load_const              42
store_name              $value
load_const              None
return_value
"""


def _make_fixture_project(root: Path) -> None:
    root.joinpath("pyproject.toml").write_text(_PYPROJECT)
    pkg = root / "pkg"
    pkg.mkdir()
    pkg.joinpath("__init__.py").write_text("")
    pkg.joinpath("mod.py").write_text('def greet():\n    return "hi"\n')
    pkg.joinpath("asm_mod.pya").write_text(_ASM_SOURCE)


@pytest.fixture
def fixture_project(tmp_path, monkeypatch):
    _make_fixture_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_build_wheel_compiles_py_and_pya_and_drops_source(fixture_project, tmp_path):  # noqa: ARG001
    dist = tmp_path / "dist"
    dist.mkdir()

    filename = buildbackend.build_wheel(str(dist))

    tag = f"cp{sys.version_info[0]}{sys.version_info[1]}-none-any"
    assert filename == f"fixture_pkg-0.1.0-{tag}.whl"

    wheel_path = dist / filename
    assert wheel_path.exists()

    with zipfile.ZipFile(wheel_path) as zf:
        names = zf.namelist()

        assert "pkg/__init__.pyc" in names
        assert "pkg/mod.pyc" in names
        assert "pkg/asm_mod.pyc" in names
        assert not any(n.endswith((".py", ".pya")) for n in names if ".dist-info/" not in n)

        wheel_meta_name = next(n for n in names if n.endswith(".dist-info/WHEEL"))
        wheel_meta = zf.read(wheel_meta_name).decode("utf-8")
        assert f"Tag: {tag}" in wheel_meta

        record_name = next(n for n in names if n.endswith(".dist-info/RECORD"))
        record = zf.read(record_name).decode("utf-8")
        record_paths = {line.split(",")[0] for line in record.splitlines() if line}
        assert record_paths == set(names)


def test_compiled_wheel_is_importable(fixture_project, tmp_path):  # noqa: ARG001
    dist = tmp_path / "dist"
    dist.mkdir()
    filename = buildbackend.build_wheel(str(dist))

    site_dir = tmp_path / "site"
    site_dir.mkdir()
    with zipfile.ZipFile(dist / filename) as zf:
        for name in zf.namelist():
            if ".dist-info/" in name:
                continue
            zf.extract(name, site_dir)

    sys.path.insert(0, str(site_dir))
    try:
        mod = importlib.import_module("pkg.mod")
        asm_mod = importlib.import_module("pkg.asm_mod")
        assert mod.greet() == "hi"
        assert asm_mod.value == 42
    finally:
        sys.path.remove(str(site_dir))
        for name in ("pkg", "pkg.mod", "pkg.asm_mod"):
            sys.modules.pop(name, None)
