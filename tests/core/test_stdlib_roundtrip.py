"""Round-trip every function in a selection of stdlib modules."""

import importlib
import inspect
import types

from spasm import _core

Bytecode = _core.Bytecode

# Pure-Python modules that are safe to import as a side effect of running the
# tests. The list is deliberately broad: this is the widest sample of
# real-world bytecode the suite has, and every module added to it is more
# opcode shapes exercised for free.
MODULES = [
    "argparse",
    "ast",
    "base64",
    "bisect",
    "calendar",
    "cmd",
    "codecs",
    "collections",
    "configparser",
    "contextlib",
    "copy",
    "csv",
    "dataclasses",
    "datetime",
    "decimal",
    "difflib",
    "dis",
    "email.message",
    "enum",
    "fnmatch",
    "fractions",
    "functools",
    "genericpath",
    "getopt",
    "gettext",
    "glob",
    "gzip",
    "hashlib",
    "heapq",
    "hmac",
    "html.parser",
    "http.client",
    "imaplib",
    "inspect",
    "io",
    "ipaddress",
    "itertools",
    "json.decoder",
    "json.encoder",
    "linecache",
    "locale",
    "logging",
    "mimetypes",
    "numbers",
    "operator",
    "optparse",
    "os",
    "pathlib",
    "pickle",
    "pkgutil",
    "platform",
    "plistlib",
    "pprint",
    "queue",
    "quopri",
    "random",
    "re",
    "reprlib",
    "secrets",
    "selectors",
    "shlex",
    "shutil",
    "smtplib",
    "socket",
    "socketserver",
    "ssl",
    "statistics",
    "string",
    "stringprep",
    "struct",
    "subprocess",
    "symtable",
    "tarfile",
    "tempfile",
    "textwrap",
    "threading",
    "timeit",
    "token",
    "tokenize",
    "traceback",
    "types",
    "typing",
    "unittest.case",
    "urllib.parse",
    "urllib.request",
    "uuid",
    "warnings",
    "wave",
    "weakref",
    "xml.etree.ElementTree",
    "zipfile",
]


def iter_code_objects(co, seen):
    """Yield `co` and, recursively, every code object nested in its consts.

    The nested ones are where comprehensions, lambdas and class bodies live, so
    skipping them would leave out most of the interesting shapes.
    """
    if id(co) in seen:
        return
    seen.add(id(co))
    yield co
    for const in co.co_consts:
        if isinstance(const, types.CodeType):
            yield from iter_code_objects(const, seen)


def iter_callables(obj, depth=0, seen=None):
    """Yield the code objects reachable from a module or class attribute."""
    # Some objects (e.g. traceback._ShutdownTheme, a __getattr__-returns-self
    # stand-in used during late interpreter shutdown on 3.15+) make every
    # attribute access — including __func__/__wrapped__/etc. — resolve back
    # to the object itself, which would otherwise recurse forever.
    if seen is None:
        seen = set()
    if id(obj) in seen:
        return
    seen = seen | {id(obj)}

    # isinstance, not just "is not None": on a class such as types.FunctionType
    # the attribute resolves to the descriptor rather than to a code object.
    co = getattr(obj, "__code__", None)
    if isinstance(co, types.CodeType):
        yield co
        return
    # Descend one level into classes: their methods are not module attributes,
    # and they are a large share of the code in most modules.
    if isinstance(obj, type) and depth == 0:
        for name in dir(obj):
            try:
                yield from iter_callables(inspect.getattr_static(obj, name), depth + 1, seen)
            except AttributeError:
                continue
    # Unwrap the descriptors that a bare getattr would hand back instead of a
    # function: staticmethod/classmethod objects, and property accessors.
    for attr in ("__func__", "__wrapped__", "fget", "fset", "fdel"):
        inner = getattr(obj, attr, None)
        if inner is not None:
            yield from iter_callables(inner, depth, seen)


def iter_module_functions(mod):
    seen = set()
    for name in dir(mod):
        try:
            obj = getattr(mod, name)
        except Exception:  # noqa: S112
            # A module attribute can raise anything on access — a lazy
            # import, a deprecation shim, a descriptor. Skip it and move on.
            continue
        for co in iter_callables(obj):
            yield from iter_code_objects(co, seen)


def line_ranges(co):
    """The line table as a list of (start, end, lineno) ranges.

    Adjacent ranges carrying the same line are merged: how a run of
    instructions is split across table entries is an encoding detail, whereas
    which line each offset maps to is not.
    """
    merged = []
    for start, end, lineno in co.co_lines():
        if merged and merged[-1][2] == lineno and merged[-1][1] == start:
            merged[-1][1] = end
        else:
            merged.append([start, end, lineno])
    return [tuple(_) for _ in merged]


def run_round_trip():
    errors = []
    total = 0

    for modname in MODULES:
        try:
            mod = importlib.import_module(modname)
        except ImportError:
            continue

        for co in iter_module_functions(mod):
            total += 1
            where = f"  {co.co_filename}:{co.co_firstlineno} {getattr(co, 'co_qualname', co.co_name)}"
            try:
                bc = Bytecode.from_code(co)
                new_co = bc.to_code()
                if new_co is None:
                    errors.append(f"{where}: to_code() returned None")
                    continue
                # Byte-for-byte identity is a stronger property than we
                # strictly need — a differently but equivalently encoded
                # instruction stream would be fine — but it holds across the
                # whole corpus on every supported version, so anything that
                # breaks it is worth looking at.
                if new_co.co_code != co.co_code:
                    errors.append(f"{where}: bytecode differs")
                # The line table is easy to get subtly wrong in ways that still
                # produce a loadable code object, so compare it explicitly.
                before, after = line_ranges(co), line_ranges(new_co)
                if before != after:
                    # Not strict: the lengths differing is itself one of the mismatches
                    # this is reporting on.
                    diff = [p for p in zip(before, after, strict=False) if p[0] != p[1]][:3]
                    errors.append(f"{where}: line table mismatch {diff}")
                # Our stack depth analysis should agree with the compiler's
                # exactly. A larger value would only waste frame space, but a
                # smaller one is a crash rather than an exception, so this is
                # the one property worth being strict about.
                if new_co.co_stacksize != co.co_stacksize:
                    errors.append(f"{where}: stacksize {co.co_stacksize} -> {new_co.co_stacksize}")
                # Same for the exception table: a handler at the wrong offset
                # or entered at the wrong depth is not a recoverable error.
                if getattr(new_co, "co_exceptiontable", b"") != getattr(co, "co_exceptiontable", b""):
                    errors.append(f"{where}: exception table differs")
            except Exception as e:
                errors.append(f"{where}: {e}")

    if errors:
        detail = "\n".join(errors[:20])
        raise AssertionError(f"FAIL (roundtrip) — {len(errors)}/{total} failures:\n{detail}")
    print(f"OK   (roundtrip) — {total} code objects")


def test_round_trip():
    run_round_trip()


if __name__ == "__main__":
    run_round_trip()
