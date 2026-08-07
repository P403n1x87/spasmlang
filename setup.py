from setuptools import setup, Extension
from pathlib import Path
import dis
import sys

ROOT = Path(__file__).parent
SRC  = ROOT / "src"

# ── Generate per-opcode cache-size table (3.11+) ──────────────────────────────
# CACHE (opcode 0) entries follow certain instructions; we strip on decode and
# re-add on encode.  Three different APIs exist across versions:
#   3.12+: dis._get_cache_size(opname) -> int
#   3.11:  dis._inline_cache_entries[opcode] -> int  (256-element list)
#   3.10:  no inline caches (HAS_CACHE_ENTRIES=0)
def _get_cache_sizes():
    if hasattr(dis, '_get_cache_size'):          # 3.12+
        return {op: dis._get_cache_size(name)
                for name, op in dis.opmap.items()
                if dis._get_cache_size(name) > 0}
    if hasattr(dis, '_inline_cache_entries'):    # 3.11
        tbl = dis._inline_cache_entries          # list indexed by opcode
        return {op: tbl[op] for op in range(min(256, len(tbl))) if tbl[op] > 0}
    return {}                                    # 3.10 — no caches

_cache_sizes = _get_cache_sizes()
_cache_gen = SRC / "cache_sizes_gen.h"
with _cache_gen.open("w") as _f:
    _f.write(f"// Auto-generated for CPython {sys.version_info.major}.{sys.version_info.minor}\n")
    _f.write("static inline int instr_cache_size(uint8_t op) noexcept {\n")
    if _cache_sizes:
        _f.write("    switch (op) {\n")
        for _op, _n in sorted(_cache_sizes.items()):
            _name = next((n for n, o in dis.opmap.items() if o == _op), str(_op))
            _f.write(f"    case {_op}: return {_n};  // {_name}\n")
        _f.write("    default: return 0;\n    }\n}\n")
        _f.write("#define HAS_CACHE_ENTRIES 1\n")
    else:
        _f.write("    (void)op; return 0;\n}\n")
        _f.write("#define HAS_CACHE_ENTRIES 0\n")

# ── Generate jump-opcode table (kind + is_jump) ───────────────────────────────
# JumpKind: NONE=0, ABS=1 (absolute), FWD=2 (forward-relative), BWD=3 (backward-relative)
# In 3.12+, hasjabs is empty; all jumps are relative.
# Backward opcodes are identified by "BACKWARD" in their name.
_abs_ops = set(getattr(dis, 'hasjabs', []))
_all_jump_ops = (set(getattr(dis, 'hasjump', []))
                 | set(getattr(dis, 'hasjabs', []))
                 | set(getattr(dis, 'hasjrel', [])))
_rel_ops = _all_jump_ops - _abs_ops
_bwd_ops = {op for name, op in dis.opmap.items()
            if op in _rel_ops and 'BACKWARD' in name}
_fwd_ops = _rel_ops - _bwd_ops

_gen = SRC / "jump_opcodes_gen.h"
with _gen.open("w") as _f:
    _f.write(f"// Auto-generated for CPython {sys.version_info.major}.{sys.version_info.minor}\n")
    _f.write("enum class JumpKind : uint8_t { NONE=0, ABS=1, FWD=2, BWD=3 };\n")
    _f.write("static inline JumpKind jump_kind(uint8_t op) noexcept {\n")
    _f.write("    switch (op) {\n")
    for _op in sorted(_abs_ops):
        _f.write(f"    case {_op}: return JumpKind::ABS;\n")
    for _op in sorted(_fwd_ops):
        _f.write(f"    case {_op}: return JumpKind::FWD;\n")
    for _op in sorted(_bwd_ops):
        _f.write(f"    case {_op}: return JumpKind::BWD;\n")
    _f.write("    default: return JumpKind::NONE;\n")
    _f.write("    }\n}\n")
    _f.write("static inline bool is_jump_opcode(uint8_t op) noexcept {\n")
    _f.write("    return jump_kind(op) != JumpKind::NONE;\n}\n")

# ── Generate arg-kind table (CONST / LOCAL / FREE / INT) ─────────────────────
# ArgKind drives abstract decode in from_code() and re-encode in to_code().
# We intentionally omit NAME (hasname) here because LOAD_GLOBAL / LOAD_ATTR
# use a shifted encoding in 3.11+ that makes the API awkward; users call
# bc.add_name() and compute the arg themselves for those opcodes.
# Some haslocal opcodes pack two variable indices into one arg
# (e.g. LOAD_FAST_LOAD_FAST, STORE_FAST_STORE_FAST, STORE_FAST_LOAD_FAST).
# These are "superinstructions" named BASE1_BASE2 where both halves are
# themselves haslocal opcodes.  Detect by structural name decomposition.
_local_opnames = {name for name, op in dis.opmap.items() if op in set(dis.haslocal)}

def _is_packed_local(opname):
    """Return True if opname = BASE1 + '_' + BASE2, both in haslocal."""
    for _base in _local_opnames:
        if opname.startswith(_base + '_'):
            _rest = opname[len(_base) + 1:]
            if _rest in _local_opnames:
                return True
    return False

_simple_local_ops = {
    _op for _name, _op in dis.opmap.items()
    if _op in set(dis.haslocal) and not _is_packed_local(_name)
}

_argkind_gen = SRC / "arg_kind_gen.h"
with _argkind_gen.open("w") as _f:
    _f.write(f"// Auto-generated for CPython {sys.version_info.major}.{sys.version_info.minor}\n")
    _f.write("enum class ArgKind : uint8_t { INT=0, CONST=1, LOCAL=2, FREE=3 };\n")
    _f.write("static inline ArgKind arg_kind(uint8_t op) noexcept {\n")
    _f.write("    switch (op) {\n")
    for _op in sorted(set(dis.hasconst)):
        _f.write(f"    case {_op}: return ArgKind::CONST;\n")
    for _op in sorted(_simple_local_ops):
        _f.write(f"    case {_op}: return ArgKind::LOCAL;\n")
    for _op in sorted(set(dis.hasfree)):
        _f.write(f"    case {_op}: return ArgKind::FREE;\n")
    _f.write("    default: return ArgKind::INT;\n")
    _f.write("    }\n}\n")

# ── Generate opcode name -> value table ───────────────────────────────────────
# Lets Instr(op, ...) accept an opname string ("LOAD_FAST") instead of
# requiring the caller to look up dis.opmap themselves.
_opname_gen = SRC / "opcode_names_gen.h"
with _opname_gen.open("w") as _f:
    _f.write(f"// Auto-generated for CPython {sys.version_info.major}.{sys.version_info.minor}\n")
    _f.write("#include <unordered_map>\n")
    _f.write("#include <string>\n")
    _f.write("static inline const std::unordered_map<std::string, uint8_t>& opcode_name_table() {\n")
    _f.write("    static const std::unordered_map<std::string, uint8_t> table = {\n")
    for _name, _op in sorted(dis.opmap.items()):
        _f.write(f'        {{"{_name}", {_op}}},\n')
    _f.write("    };\n")
    _f.write("    return table;\n}\n")

# ── Generate stack-depth opcode classification tables ─────────────────────────
# compute_stacksize() (stackdepth.cpp) walks the instruction graph computing
# per-instruction stack effects via the running interpreter's own
# _opcode.stack_effect(), so it never needs a hand-maintained push/pop table.
# It does need four small per-opcode classifications, generated here:
#   opcode_has_arg   - does _opcode.stack_effect() accept/require an oparg for
#                       this op? (varies by version in ways dis.hasarg/
#                       HAVE_ARGUMENT don't reliably predict — determined
#                       empirically against the actual function we'll call.)
#   is_unconditional_jump - a jump with no fallthrough edge (JUMP_FORWARD,
#                       JUMP_BACKWARD, ...) vs. a conditional one
#                       (POP_JUMP_IF_*, FOR_ITER, SEND, ...) that has both.
#   is_scope_exit     - terminates the block with neither a fallthrough nor
#                       a jump edge (RETURN_VALUE, RETURN_CONST, RAISE_VARARGS,
#                       RERAISE).
#   is_stackdepth_neutral - the generator-entry marker every generator's
#                       bytecode starts with (GEN_START on 3.10;
#                       RETURN_GENERATOR on 3.11+). Its _opcode.stack_effect
#                       is nonzero (accounting for what happens when the
#                       function is *called*), but the generator body that
#                       follows always starts fresh at depth 0 when later
#                       resumed — CPython's own compiler doesn't count this
#                       marker's effect toward co_stacksize, so neither do we.
import _opcode as _opcode_mod

def _opcode_accepts_oparg(op):
    try:
        _opcode_mod.stack_effect(op, 0)
        return True
    except (ValueError, SystemError):
        return False

# Instr::op is a uint8_t — pseudo-opcodes (>255, e.g. JUMP=256 on 3.13+)
# can never actually appear in a decoded instruction, so exclude them here
# to avoid out-of-range switch cases in the generated C++.
_has_arg = {op: _opcode_accepts_oparg(op) for op in dis.opmap.values() if op <= 255}

_UNCONDITIONAL_JUMP_NAMES = {
    "JUMP", "JUMP_NO_INTERRUPT", "JUMP_FORWARD",
    "JUMP_BACKWARD", "JUMP_BACKWARD_NO_INTERRUPT", "JUMP_ABSOLUTE",
}
_SCOPE_EXIT_NAMES = {"RETURN_VALUE", "RETURN_CONST", "RAISE_VARARGS", "RERAISE"}
_STACKDEPTH_NEUTRAL_NAMES = {"GEN_START", "RETURN_GENERATOR"}

_unconditional_ops = {op for name, op in dis.opmap.items() if name in _UNCONDITIONAL_JUMP_NAMES and op <= 255}
_scope_exit_ops    = {op for name, op in dis.opmap.items() if name in _SCOPE_EXIT_NAMES and op <= 255}
_neutral_ops       = {op for name, op in dis.opmap.items() if name in _STACKDEPTH_NEUTRAL_NAMES and op <= 255}
_pop_top_op        = dis.opmap["POP_TOP"]

# opcode.MIN_INSTRUMENTED_OPCODE exists from 3.12 on; before that there are no
# instrumented opcodes at all, so nothing qualifies.
import opcode as _opcode
_min_instrumented = getattr(_opcode, "MIN_INSTRUMENTED_OPCODE", 1 << 30)

_stackdepth_gen = SRC / "stackdepth_opcodes_gen.h"
with _stackdepth_gen.open("w") as _f:
    _f.write(f"// Auto-generated for CPython {sys.version_info.major}.{sys.version_info.minor}\n")

    _f.write("static inline bool opcode_has_arg(uint8_t op) noexcept {\n")
    _f.write("    switch (op) {\n")
    for _op in sorted(op for op, ok in _has_arg.items() if ok):
        _f.write(f"    case {_op}: return true;\n")
    _f.write("    default: return false;\n    }\n}\n")

    _f.write("static inline bool is_unconditional_jump(uint8_t op) noexcept {\n")
    _f.write("    switch (op) {\n")
    for _op in sorted(_unconditional_ops):
        _f.write(f"    case {_op}: return true;\n")
    _f.write("    default: return false;\n    }\n}\n")

    _f.write("static inline bool is_scope_exit(uint8_t op) noexcept {\n")
    _f.write("    switch (op) {\n")
    for _op in sorted(_scope_exit_ops):
        _f.write(f"    case {_op}: return true;\n")
    _f.write("    default: return false;\n    }\n}\n")

    # RETURN_GENERATOR (3.11+) is always immediately followed by a cleanup
    # POP_TOP in CPython's own generated prologue — that POP_TOP's pop must
    # *also* be treated as neutral (the pair is net-zero for the real body
    # that follows), or the whole function's depth ends up permanently
    # offset by -1. GEN_START (3.10) has no such follow-up pop.
    _f.write(f"static constexpr uint8_t POP_TOP_OPCODE = {_pop_top_op};\n")

    _f.write("static inline bool is_stackdepth_neutral(uint8_t op) noexcept {\n")
    _f.write("    switch (op) {\n")
    for _op in sorted(_neutral_ops):
        _f.write(f"    case {_op}: return true;\n")
    _f.write("    default: return false;\n    }\n}\n")

    # Opcodes the interpreter writes into a code object at runtime and that
    # cannot appear in one being built: the INSTRUMENTED_* family that
    # sys.monitoring patches in, and the JIT's ENTER_EXECUTOR. Handing some of
    # them to PyCode_New crashes the interpreter outright — it dereferences
    # monitoring state that a fresh code object doesn't have — so they are
    # rejected rather than encoded. Pseudo-opcodes need no entry here: they are
    # all above 255 and so cannot be held by an Instr in the first place.
    _internal_ops = {op for name, op in dis.opmap.items()
                     if op <= 255 and (op >= _min_instrumented or name in ("ENTER_EXECUTOR", "RESERVED"))}
    _f.write("static inline bool is_internal_opcode(uint8_t op) noexcept {\n")
    _f.write("    switch (op) {\n")
    for _op in sorted(_internal_ops):
        _name = next((n for n, o in dis.opmap.items() if o == _op), str(_op))
        _f.write(f"    case {_op}: return true;  // {_name}\n")
    _f.write("    default: return false;\n    }\n}\n")

# Touch the C++ sources so setuptools always recompiles after header regeneration.
import os as _os, time as _time
_now = _time.time()
for _src in ["bytecode.cpp", "module.cpp", "stackdepth.cpp"]:
    _path = str(SRC / _src)
    _os.utime(_path, (_now, _now))

# MSVC (cl.exe) doesn't understand GCC/Clang flags like -std=c++20, and
# requires C++20 (not just C++17) to accept the designated initializers used
# in module.cpp.
if sys.platform == "win32":
    extra_compile_args = ["/std:c++20", "/O2", "/W4"]
else:
    extra_compile_args = ["-std=c++20", "-O2", "-Wall", "-Wextra"]

ext = Extension(
    "spasm._core",
    # distutils requires paths relative to setup.py, not absolute.
    sources=[str((SRC / f).relative_to(ROOT)) for f in (
        "module.cpp",
        "bytecode.cpp",
        "linetable.cpp",
        "exctable.cpp",
        "stackdepth.cpp",
    )],
    include_dirs=[str(SRC.relative_to(ROOT))],
    extra_compile_args=extra_compile_args,
    language="c++",
)

# Everything else (name, version, deps) comes from pyproject.toml; this file
# exists only to generate the per-interpreter headers and build the extension.
setup(ext_modules=[ext])
