"""A PEP 517 build backend that wraps another backend and compiles its wheel.

This is not a from-scratch wheel builder: metadata, versioning, sdist
contents, namespace packages, editable installs — everything a real backend
already does well — is delegated verbatim to whichever backend
``[tool.spasm.build] backend`` names (``hatchling.build`` by default). The
only thing this module adds is a post-processing pass over the wheel that
backend produces: every ``.py``/``.pya`` file inside it is compiled to a bare
``.pyc`` and the source is dropped, so the distributed wheel ships compiled
bytecode only.

A project opts in with:

    [build-system]
    requires = ["spasmlang[buildbackend]", "hatchling"]
    build-backend = "spasm.buildbackend"

pyc content is interpreter-version-specific (magic number) but has no C ABI
dependency, so the wheel this produces is retagged ``cp{XY}-none-any`` —
one wheel per Python minor version, any platform.
"""

import base64
import fnmatch
import hashlib
import importlib
import sys
import zipfile
from pathlib import Path
from types import CodeType
from types import ModuleType

from spasm._asm import Assembly
from spasm._pyc import code_to_pyc_bytes

try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]
    except ImportError:
        msg = (
            "spasm.buildbackend needs a TOML parser on Python <3.11. Add "
            "'spasmlang[buildbackend]' instead of 'spasmlang' to your "
            "[build-system] requires."
        )
        raise ImportError(msg) from None

__all__ = [
    "build_editable",
    "build_sdist",
    "build_wheel",
    "get_requires_for_build_editable",
    "get_requires_for_build_sdist",
    "get_requires_for_build_wheel",
    "prepare_metadata_for_build_editable",
    "prepare_metadata_for_build_wheel",
]

_DEFAULT_BACKEND = "hatchling.build"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _load_config() -> tuple[ModuleType, dict]:
    with Path("pyproject.toml").open("rb") as f:
        data = tomllib.load(f)

    cfg = data.get("tool", {}).get("spasm", {}).get("build", {})
    backend_name = cfg.get("backend", _DEFAULT_BACKEND)

    return importlib.import_module(backend_name), cfg


def _is_selected(name: str, include: list[str] | None, exclude: list[str] | None) -> bool:
    if include and not any(fnmatch.fnmatch(name, pattern) for pattern in include):
        return False
    return not (exclude and any(fnmatch.fnmatch(name, pattern) for pattern in exclude))


# ---------------------------------------------------------------------------
# Wheel post-processing
# ---------------------------------------------------------------------------


def _compile_source(name: str, data: bytes, optimize: int) -> CodeType:
    if name.endswith(".pya"):
        asm = Assembly(name="<module>", filename=name, lineno=1)
        asm.parse(data.decode("utf-8"))
        return asm.compile()

    return compile(data, name, "exec", optimize=optimize)


def _retag_wheel_metadata(data: bytes, tag: str) -> bytes:
    lines = [line for line in data.decode("utf-8").splitlines() if not line.startswith("Tag:")]
    lines.append(f"Tag: {tag}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _retag_wheel_filename(filename: str, tag: str) -> str:
    stem = filename[: -len(".whl")]
    parts = stem.split("-")
    # {name}-{version}[-{build}]-{pytag}-{abitag}-{platformtag}.whl — the new
    # tag always replaces exactly the trailing pytag/abitag/platformtag triple.
    return "-".join([*parts[:-3], *tag.split("-")]) + ".whl"


def _build_record(names: list[str], contents: dict[str, bytes], record_name: str) -> bytes:
    lines = []
    for name in names:
        if name == record_name:
            lines.append(f"{name},,")
            continue
        data = contents[name]
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
        lines.append(f"{name},sha256={digest},{len(data)}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _compile_wheel(wheel_path: Path, cfg: dict) -> str:
    include = cfg.get("include")
    exclude = cfg.get("exclude")
    optimize = cfg.get("optimize", 0)

    with zipfile.ZipFile(wheel_path) as zin:
        names = [info.filename for info in zin.infolist() if not info.filename.endswith("/")]
        contents = {name: zin.read(name) for name in names}

    dist_info_wheel = next((n for n in names if n.endswith(".dist-info/WHEEL")), None)

    new_order = []
    for name in names:
        if (
            (name.endswith(".py") or name.endswith(".pya"))
            and ".dist-info/" not in name
            and _is_selected(name, include, exclude)
        ):
            code = _compile_source(name, contents.pop(name), optimize)
            pyc_name = name.rsplit(".", 1)[0] + ".pyc"
            contents[pyc_name] = code_to_pyc_bytes(code)
            new_order.append(pyc_name)
        else:
            new_order.append(name)

    tag = f"cp{sys.version_info[0]}{sys.version_info[1]}-none-any"

    if dist_info_wheel is not None:
        contents[dist_info_wheel] = _retag_wheel_metadata(contents[dist_info_wheel], tag)

    record_name = next((n for n in new_order if n.endswith(".dist-info/RECORD")), None)
    if record_name is not None:
        contents[record_name] = _build_record(new_order, contents, record_name)

    new_filename = _retag_wheel_filename(wheel_path.name, tag) if dist_info_wheel is not None else wheel_path.name

    tmp_path = wheel_path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name in new_order:
            zout.writestr(name, contents[name])

    final_path = wheel_path.parent / new_filename
    if wheel_path != final_path:
        wheel_path.unlink()
    tmp_path.replace(final_path)

    return new_filename


# ---------------------------------------------------------------------------
# PEP 517 hooks
# ---------------------------------------------------------------------------


def get_requires_for_build_wheel(config_settings: dict | None = None) -> list[str]:
    backend, _ = _load_config()
    if not hasattr(backend, "get_requires_for_build_wheel"):
        return []
    return list(backend.get_requires_for_build_wheel(config_settings))


def get_requires_for_build_sdist(config_settings: dict | None = None) -> list[str]:
    backend, _ = _load_config()
    if not hasattr(backend, "get_requires_for_build_sdist"):
        return []
    return list(backend.get_requires_for_build_sdist(config_settings))


def build_sdist(sdist_directory: str, config_settings: dict | None = None) -> str:
    backend, _ = _load_config()
    return str(backend.build_sdist(sdist_directory, config_settings))


def prepare_metadata_for_build_wheel(
    metadata_directory: str,
    config_settings: dict | None = None,
) -> str:
    backend, _ = _load_config()
    if not hasattr(backend, "prepare_metadata_for_build_wheel"):
        msg = f"{backend.__name__} does not implement prepare_metadata_for_build_wheel"
        raise NotImplementedError(msg)
    return str(backend.prepare_metadata_for_build_wheel(metadata_directory, config_settings))


def build_wheel(
    wheel_directory: str,
    config_settings: dict | None = None,
    metadata_directory: str | None = None,
) -> str:
    backend, cfg = _load_config()
    filename = backend.build_wheel(
        wheel_directory,
        config_settings=config_settings,
        metadata_directory=metadata_directory,
    )
    return _compile_wheel(Path(wheel_directory) / filename, cfg)


# Editable installs are passed straight through, uncompiled: there is no
# wheel artifact to post-process, only a set of .pth/redirect files pointing
# back at the source tree.


def get_requires_for_build_editable(config_settings: dict | None = None) -> list[str]:
    backend, _ = _load_config()
    if not hasattr(backend, "get_requires_for_build_editable"):
        return []
    return list(backend.get_requires_for_build_editable(config_settings))


def prepare_metadata_for_build_editable(
    metadata_directory: str,
    config_settings: dict | None = None,
) -> str:
    backend, _ = _load_config()
    if not hasattr(backend, "prepare_metadata_for_build_editable"):
        msg = f"{backend.__name__} does not implement prepare_metadata_for_build_editable"
        raise NotImplementedError(msg)
    return str(backend.prepare_metadata_for_build_editable(metadata_directory, config_settings))


def build_editable(
    wheel_directory: str,
    config_settings: dict | None = None,
    metadata_directory: str | None = None,
) -> str:
    backend, _ = _load_config()
    if not hasattr(backend, "build_editable"):
        msg = f"{backend.__name__} does not implement build_editable"
        raise NotImplementedError(msg)
    return str(
        backend.build_editable(
            wheel_directory,
            config_settings=config_settings,
            metadata_directory=metadata_directory,
        )
    )
