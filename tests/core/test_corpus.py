"""
Corpus test: compile every .py file in a directory tree, then recursively
decompile + recompile every nested code object with bytecode-native.

Usage:
    python tests/test_corpus.py [root_dir]

Defaults to ../dogweb relative to this file's parent.
"""

import sys
import types
from collections import defaultdict
from pathlib import Path

from spasm import _core

ROOT = Path(__file__).parent.parent

Bytecode = _core.Bytecode


# ── Helpers ───────────────────────────────────────────────────────────────────


def iter_code_objects(co, seen=None):
    """Yield co and every code object nested inside it, recursively."""
    if seen is None:
        seen = set()
    if id(co) in seen:
        return
    seen.add(id(co))
    yield co
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            yield from iter_code_objects(c, seen)


def compile_file(path: Path):
    """Return the top-level code object for a .py file, or None on syntax error."""
    try:
        src = path.read_bytes()
        return compile(src, str(path), "exec", optimize=0, dont_inherit=True)
    except SyntaxError:
        return None
    except Exception:
        return None


def decompile_recompile(co):
    """
    Run from_code() -> to_code() on co and return (ok, error_msg).
    Does NOT execute the result — just checks assembly roundtrip.
    """
    try:
        bc = Bytecode.from_code(co)
        bc.to_code()
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ── Main ──────────────────────────────────────────────────────────────────────


def run(corpus_root: Path):
    py_files = sorted(corpus_root.rglob("*.py"))
    total_files = len(py_files)
    skip_syntax = 0
    total_cos = 0
    failed_cos = 0
    errors = defaultdict(list)  # error_type → [(path, qualname, msg)]

    for path in py_files:
        top = compile_file(path)
        if top is None:
            skip_syntax += 1
            continue

        rel = path.relative_to(corpus_root)
        for co in iter_code_objects(top):
            total_cos += 1
            ok, msg = decompile_recompile(co)
            if not ok:
                failed_cos += 1
                etype = msg.split(":")[0]
                errors[etype].append((str(rel), co.co_qualname, msg))

    # ── Report ────────────────────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"Corpus: {corpus_root}")
    print(f"{'=' * 70}")
    print(f"  Python files found : {total_files:>8,}")
    print(f"  Skipped (syntax)   : {skip_syntax:>8,}")
    print(f"  Code objects tested: {total_cos:>8,}")
    print(f"  Failures           : {failed_cos:>8,}")
    if total_cos:
        print(f"  Pass rate          : {(total_cos - failed_cos) / total_cos * 100:>7.2f}%")
    print()

    if errors:
        print("Failure breakdown:")
        for etype, cases in sorted(errors.items(), key=lambda x: -len(x[1])):
            print(f"  {etype}: {len(cases)} failures")
        print()
        # Show up to 5 examples per error type
        for etype, cases in sorted(errors.items(), key=lambda x: -len(x[1])):
            print(f"  [{etype}] examples:")
            for path, qualname, msg in cases[:5]:
                print(f"    {path}  {qualname}")
                print(f"      {msg}")
            if len(cases) > 5:
                print(f"    ... and {len(cases) - 5} more")
            print()
    else:
        print("  All code objects passed! ✓")

    return failed_cos == 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        corpus = Path(sys.argv[1])
    else:
        corpus = ROOT.parent / "dogweb"

    if not corpus.exists():
        print(f"ERROR: corpus root not found: {corpus}", file=sys.stderr)
        sys.exit(1)

    ok = run(corpus)
    sys.exit(0 if ok else 1)
