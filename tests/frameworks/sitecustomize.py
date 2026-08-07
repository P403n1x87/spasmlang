"""Import hook: round-trips every code object through spasm._core.

Mirrors the bytecode library's own framework test setup exactly, replacing
the CFG-based round-trip with spasm._core's from_code/to_code.
"""

import atexit
import dis
import io
import sys
from datetime import timedelta
from pathlib import Path
from time import monotonic as time
from types import CodeType
from types import ModuleType

# Resolve spasm from the project root (two levels up from this file)
_PROJECT_ROOT = str(Path(__file__).parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from module import BaseModuleWatchdog  # type: ignore  (same dir, like bytecode's setup)

from spasm._core import Bytecode

_original_exec = exec


class BytecodeNativeError(Exception):
    def __init__(self, message, code, exc=None):
        stream = io.StringIO()
        print(message, file=stream)
        if exc is not None:
            import traceback

            traceback.print_exception(type(exc), exc, exc.__traceback__, file=stream)
        try:
            dis.dis(code, file=stream, depth=0, show_caches=True)
        except Exception:
            pass
        super().__init__(stream.getvalue())


class ModuleCodeCollector(BaseModuleWatchdog):
    def __init__(self):
        super().__init__()
        self.count = 0
        self.stopwatch = 0.0

        # Intercept exec() used by pytest's assertion rewriter
        try:
            import _pytest.assertion.rewrite as par

            par.exec = self._exec
        except ImportError:
            pass

    def transform(self, code: CodeType, _module: ModuleType, root: bool = True) -> CodeType:
        try:
            start = time()
            bc = Bytecode.from_code(code)
        except Exception as e:
            raise BytecodeNativeError(f"from_code failed for {code!r} in {_module}", code, e) from e

        try:
            # Recursively transform nested code objects (lambdas, comprehensions,
            # inner functions, class bodies) in-place so co_consts indices are
            # preserved — no new entries appended, no EXTENDED_ARG inflation.
            code_map: dict = {}
            for c in bc.consts:
                if isinstance(c, CodeType) and c not in code_map:
                    code_map[c] = self.transform(c, _module, root=False)
            for i, c in enumerate(bc.consts):
                if c in code_map:
                    bc.consts[i] = code_map[c]
            for instr in bc.instrs:
                if isinstance(instr.arg, CodeType) and instr.arg in code_map:
                    instr.arg = code_map[instr.arg]

            recompiled = bc.to_code()
            if recompiled is None:
                raise RuntimeError("to_code() returned None")

            # Verify co_positions() count == bytecode words (3.11+)
            if sys.version_info >= (3, 11):
                expected = len(recompiled.co_code) // 2
                actual = sum(1 for _ in recompiled.co_positions())
                if actual != expected:
                    import marshal

                    dump_path = f"/tmp/spasm-mismatch-{id(code)}.marshal"
                    with open(dump_path, "wb") as fh:
                        marshal.dump(code, fh)
                    raise RuntimeError(
                        f"co_positions() count mismatch: got {actual}, "
                        f"expected {expected} for {recompiled!r}; "
                        f"original code object dumped to {dump_path}"
                    )

            # Verify the result is disassemble-able
            dis.dis(recompiled, file=io.StringIO())

            if root:
                self.stopwatch += time() - start
            self.count += 1
            return recompiled
        except BytecodeNativeError:
            raise
        except Exception as e:
            raise BytecodeNativeError(f"to_code failed for {code!r} in {_module}", code, e) from e

    def after_import(self, _module: ModuleType) -> None:
        pass

    def _exec(self, _object, _globals=None, _locals=None, **kwargs):
        new_object = (
            self.transform(_object, None)
            if isinstance(_object, CodeType) and _object.co_name == "<module>"
            else _object
        )
        _original_exec(new_object, _globals, _locals, **kwargs)

    @classmethod
    def uninstall(cls) -> None:
        try:
            import _pytest.assertion.rewrite as par

            par.exec = _original_exec  # type: ignore
        except ImportError:
            pass

        inst = cls._instance
        n = inst.count if inst else 0
        t = inst.stopwatch if inst else 0.0
        print(f"[spasm] Recompiled {n} code objects in {timedelta(seconds=t)}", file=sys.stderr)
        return super().uninstall()


print("[spasm] Import hook installed", file=sys.stderr)
ModuleCodeCollector.install()


@atexit.register
def _():
    ModuleCodeCollector.uninstall()
