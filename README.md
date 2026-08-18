# spasmlang

[![PyPI - Version](https://img.shields.io/pypi/v/spasmlang.svg)](https://pypi.org/project/spasmlang)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/spasmlang.svg)](https://pypi.org/project/spasmlang)


## Synopsis

`spasmlang` is a **s**imple **P**ython **as**se**m**bly **lang**uage. It lets you
generate CPython bytecode from a simple assembly-like syntax, and assembles it
with a bundled C++ extension rather than a pure-Python bytecode library.

Supports CPython 3.10 through 3.14, and has no runtime dependencies.

-----

**Table of Contents**

- [Installation](#installation)
- [Usage](#usage)
- [Examples](#examples)
- [In-source assembly](#in-source-assembly)
- [Bytecode inlining](#bytecode-inlining)
- [Build backend](#build-backend)
- [Low-level API](#low-level-api)
- [Architecture](#architecture)
- [License](#license)


## Installation

```console
pip install spasmlang
```

Wheels are published for CPython 3.10–3.14 on Linux, macOS and Windows.
Installing from source needs a C++17 compiler.

Plain `spasmlang` has no runtime dependencies. The [build backend](#build-backend)
is the one feature that needs a TOML parser on Python <3.11, so it lives
behind an extra rather than being pulled in for everyone:

```console
pip install "spasmlang[buildbackend]"
```


## Usage

The `spasmlang` package provides a class, `Assembly`, that allows you to
generate bytecode from a simple assembly-like syntax. See the [examples](#examples)
below for a taste of its API. Where source-level assembly isn't practical, the
[low-level API](#low-level-api) lets you string instruction objects together
directly.

You can also use the `spasm` command-line utility to compile assembly files
directly to Python bytecode:

```console
spasm example.pya  # generates example.pyc
```


## Examples

This is how the classic "Hello, World!" program looks like, targeting the
CPython 3.12 bytecode:

```python
from spasm import Assembly

asm = Assembly()
asm.parse(
    r"""
    push_null
    load_const          print
    load_const          "Hello, World!"
    call                1
    return_value
    """
)
exec(asm.compile())
```

This is how you can compile the file `example.pya` to `example.pyc` to create
a "Hello, World!" module, again targeting CPython 3.11:

```
# example.pya
    resume      0
    push_null
    load_name   $print
    load_const  "Hello, spasm!"
    precall     1
    call        1
    pop_top
    load_const  None
    return_value
```

Compile the assembly code with (assuming that you have installed `spasmlang`
with CPython 3.11)
    
```console
spasm example.pya
```

and then execute the generated module with e.g.
    
```console
python3.11 -m example
```

This example shows how to create a module that exports a `greet` function that
takes one argument, targeting CPython 3.11:

```
# greet.pya

code greet(who)
    resume                      0
    load_global                 (True, "print")
    load_const                  "Hello, "
    load_fast                   $who
    format_value                0
    build_string                2
    precall                     1
    call                        1
    return_value
end

    resume 0
    load_const                  .greet
    make_function               0
    store_name                  $greet
    load_const                  None
    return_value
```

Again, compile the assembly code with

```console
spasm greet.pya
```

and test it with

```console
$ python3.11 -c "from greet import greet; greet('spasmlang')"
Hello, spasmlang
```

Code blocks nest, and a block header can declare the variables the block
shares with the one around it, which is what a closure needs:

```
code NAME(arguments)[cells]<frees>
```

The two trailing groups are optional and independent, so `code f(x)<n>`
declares a free variable and no cells. *Cells* are this block's locals that a
nested block captures; *frees* are the ones it captures from its own enclosing
block. They have to be written down because nothing in the instruction stream
tells the two apart — `LOAD_DEREF $n` looks the same either way.

This is `lambda n: lambda: n` in assembly, targeting CPython 3.12:

```
# adder.pya

code outer(n)[n]
    make_cell                   $n
    resume                      0

    code inner()<n>
        copy_free_vars          1
        resume                  0
        load_deref              $n
        return_value
    end

    load_closure                $n
    build_tuple                 1
    load_const                  .inner
    make_function               8
    return_value
end

    resume                      0
    load_const                  .outer
    make_function               0
    store_name                  $outer
    load_const                  None
    return_value
```

```console
$ spasm adder.pya && python3.12 -c "from adder import outer; print(outer(42)())"
42
```

Note that `n` appears both as an argument of `outer` and in its cell list: from
CPython 3.11 a captured parameter occupies a single frame slot that is at once
a local and a cell, and `MAKE_CELL` is what turns it into one. `inner` is
reachable only from `outer`, since a nested block is a constant of the block it
is written in and not of the module.


## In-source assembly

Writing assembly in its own `.pya` file works well for a whole module, but
sometimes only one function needs to drop to bytecode and the rest of the
file is ordinary Python. `spasm.asm` lets that function keep its assembly
right where it is defined, in its own docstring:

```python
import spasm

@spasm.asm(retval=42)
def answer():
    """
    resume 0
    load_const {retval}
    return_value
    """

assert answer() == 42
```

At decoration time, `asm` parses the docstring as assembly (through the same
`Assembly` the `spasm` CLI and `.pya` files use), compiles it, replaces
`answer.__code__` with the result, and clears `__doc__` — it held source, not
documentation.

This one has two different, easily-confused kinds of "argument", so it's
worth spelling out which is which:

| | Resolved when | Reaches the assembly as |
| --- | --- | --- |
| `answer`'s own parameters (`def add(x, y):`) | Every call, from the caller's arguments | `load_fast`/`store_fast` on the parameter name, exactly as in a nested `code` block in a `.pya` file |
| The decorator's keyword arguments (`retval=42` above) | Once, when `@spasm.asm(...)` runs | `{name}` placeholders in the docstring, substituted before it's assembled |

The first is what `def add(x, y): ...` already means in Python — nothing
`asm`-specific about it. The second is `Assembly.compile()`'s existing
bind-args mechanism (the same one `.pya` files use), exposed here so the
docstring can embed a compile-time constant without hardcoding it. It's easy
to reach for the decorator's keyword arguments out of habit and expect them
to behave like a call's arguments; they don't — they're baked into the
compiled bytecode once, not read fresh from a call:

```python
@spasm.asm()
def add(x, y):
    """
    resume 0
    load_fast $x
    load_fast $y
    binary_op 0
    return_value
    """

assert add(3, 4) == 7
```

`add`'s parameters (`x`, `y`) come straight from its own `def` line, so
there's no `code NAME(args)` header to write. There is also no closure
support: `*args`, `**kwargs`, keyword-only arguments and free/cell variables
all raise `ValueError` at decoration time. A function that needs any of
those still has the nested `code` block syntax available in a `.pya` file.


## Bytecode inlining

`spasm.inline` is a decorator for the *caller*, not the callee. A
callee-side decorator would need to find and rewrite every call site across
the codebase that happens to call it — other modules, other decorators
already applied to those callers, code not even loaded yet — which is not
something a decorator running once at callee-definition time can do.
Decorating the caller sidesteps that entirely: the caller already knows
which call sites are its own, so `inline` only ever rewrites the one code
object it's attached to. It needs no cooperation from the function being
inlined, only proof — checked structurally against the callee's own
`__code__` — that inlining it is safe:

```python
import dis
import spasm

def add_one(x):
    return x + 1

@spasm.inline
def compute(n):
    return add_one(n) * 2

assert compute(5) == 12
assert not any(instr.opname.startswith("CALL") for instr in dis.get_instructions(compute))
```

At decoration time, it walks `compute`'s bytecode for call sites shaped like
a module-level function call with a fixed number of simple positional
arguments (`LOAD_GLOBAL` + plain `LOAD_FAST`/`LOAD_CONST` pushes + `CALL`),
resolves the name against `compute.__globals__`, and checks the callee's own
`__code__` for anything that would make splicing it in unsafe: a generator or
coroutine, `*args`/`**kwargs`, a closure, an exception table, or a call to
itself. Anything it can prove safe gets its body spliced directly into the
caller in place of the call, with every `return` becoming a jump to a shared
point after the splice. Anything it can't — a keyword argument, a starred
call, a generator callee — is left exactly as it was, so `inline` is a pure
optimization: it never changes what a caller returns, only whether it still
contains a `CALL`.

A parameter the callee never reassigns, pushed by a single `LOAD_FAST` or
`LOAD_CONST` in the caller, is substituted directly at each of its use sites
instead of round-tripping through a dedicated local — so `add_one`'s `x`
above becomes `n` directly rather than a copy of it. Only a callee-local
variable computed inside the callee's own body still needs a slot of its own
in the caller.

On a tight loop calling a several-line function once per iteration, this
typically shaves 10–15% off the loop's running time — call overhead is a
meaningful fraction of a small function's cost, but not the only cost, so
`inline` narrows that gap rather than closing it. Functions with only a
handful of call sites, called rarely, will not see a measurable difference,
and that's fine: `inline` returns a caller unchanged when it finds nothing
worth splicing.


## Build backend

`spasm.buildbackend` is a [PEP 517](https://peps.python.org/pep-0517/) build
backend wrapper: it builds the wheel exactly as some other backend (hatchling
by default) already would, then rewrites that wheel so every `.py`/`.pya`
file inside it is replaced with a compiled, sourceless `.pyc`. Everything
else — metadata, versioning, sdist contents, editable installs — is forwarded
to the wrapped backend untouched.

```toml
[build-system]
requires = ["spasmlang[buildbackend]", "hatchling"]
build-backend = "spasm.buildbackend"

[tool.spasm.build]
backend = "hatchling.build"   # optional, this is the default
include = ["mypkg/*"]         # optional glob allowlist
exclude = ["mypkg/generated/*"]  # optional glob denylist
optimize = 0                  # optional, compile() optimization level
```

Because a `.pyc`'s magic number is interpreter-version-specific but nothing
inside it is platform-specific, the wheel this produces is retagged
`cp{XY}-none-any` — one wheel per Python minor version, any platform — rather
than whatever platform tag the wrapped backend chose.

C extensions inside the wrapped wheel are left untouched (only `.py`/`.pya`
is compiled), and editable installs are passed through uncompiled entirely:
there is no wheel artifact to post-process there, only `.pth`/redirect files
pointing back at the source tree.


## Low-level API

Writing a snippet of assembly is the shortest way to get bytecode, but it is
not always the right shape for the job. When the instruction stream is being
computed rather than written — you are transforming an existing code object,
generating a sequence whose length depends on runtime data, or injecting
instrumentation into somebody else's function — you want to string instructions
together as objects. That is what `spasm.bytecode` exposes, and it is the same
layer the assembler itself is built on.

```python
from spasm.bytecode import Bytecode, ExcEntry, Instr, Label
```

### The data model

A `Bytecode` is a mutable, decoded code object. `Bytecode.from_code(co)` builds
one from an existing code object and `bc.to_code()` encodes it back; the round
trip is lossless, so decoding and re-encoding an untouched function gives back
byte-identical `co_code`, line table and exception table.

| Attribute | Meaning |
| --- | --- |
| `instrs` | The instruction list. Holds `Instr` objects and nothing else — no labels, no pseudo-entries. |
| `end_labels` | Labels for the position one past the last instruction. |
| `exc_entries` | The `ExcEntry` list making up the exception table (3.11+). |
| `consts`, `names`, `varnames`, `freevars`, `cellvars` | The code object's tables, as plain lists. |
| `argcount`, `flags`, `firstlineno`, `filename`, `name`, `qualname` | The remaining code object fields. |

An `Instr` carries `op` (settable as an opname string or as an int), `arg`, and
the position attributes `lineno`, `col_offset`, `end_lineno` and `end_col`.

There is no `stacksize`: `co_stacksize` is always computed from the instruction
stream by `to_code()`, and is neither stored nor settable.

`Bytecode()` with no arguments is an empty template — no instructions, empty
tables, `argcount` 0, `flags` 0, `firstlineno` 1, `filename` `<string>`,
`name` and `qualname` `<bytecode>` — so building from scratch is a matter of
filling in the fields you care about. `Bytecode(instrs)` seeds the instruction
list in one go; either way `bc.instrs` is a live list you can mutate in place.

```python
import sys
import types

from spasm.bytecode import Bytecode, Instr, infer_flags

bc = Bytecode()
if sys.version_info >= (3, 11):
    bc.instrs.append(Instr("RESUME", 0))
bc.instrs += [
    Instr("LOAD_FAST", "x"),
    Instr("LOAD_FAST", "y"),
    Instr("BINARY_OP" if sys.version_info >= (3, 11) else "BINARY_ADD", 0),
    Instr("RETURN_VALUE"),
]
bc.name = bc.qualname = "add"
bc.argcount = 2
bc.varnames = ["x", "y"]
bc.flags = infer_flags(bc, is_function=True)

add = types.FunctionType(bc.to_code(), {})
assert add(1, 2) == 3
```

`infer_flags` derives the `co_flags` a code object being built from scratch
needs, as far as that can be done from the code alone: it cannot see
`*args`/`**kwargs` or lexical nesting, so `CO_VARARGS`, `CO_VARKEYWORDS` and
`CO_NESTED` are left to the caller.

### Instruction arguments

Arguments that name a table entry are given as the value, not as the index, and
the entry is created if it isn't there already:

| Argument kind | What to pass |
| --- | --- |
| `co_consts` (`LOAD_CONST`, …) | The constant itself. |
| `co_varnames` (`LOAD_FAST`, `STORE_FAST`, …) | The variable name, as a string. |
| free/cell variables (`LOAD_DEREF`, …) | The variable name, as a string. |
| Jump targets | A `Label`. |
| `co_names` (`LOAD_GLOBAL`, `LOAD_ATTR`, `STORE_NAME`, …) | An `int`, from `encode_name_arg()`. |
| Everything else | The raw `int` oparg. |

Name arguments are the one exception, because they are not just an index: from
3.11 `LOAD_GLOBAL` and from 3.12 `LOAD_ATTR` pack a flag bit alongside it.
`encode_name_arg(bc, opname, name, flag=False)` interns the name and returns the
oparg, shifting the index where the version calls for it.

Two more helpers cover opargs that are conceptually symbolic:
`compare_oparg(Compare.LT)` for `COMPARE_OP`, whose encoding moved twice
between 3.10 and 3.13, and the `BinaryOp` enum for `BINARY_OP` on 3.11+, whose
members are used as the oparg directly.

If you would rather work with indices, `add_const()`, `add_name()` and
`add_varname()` intern a value and hand back its index.

The superinstructions that pack two variable indices into a single oparg —
`LOAD_FAST_LOAD_FAST` and friends — take a plain `int`, since a single name
cannot express what they encode.

### Jumps and labels

Jump targets are `Label` objects rather than offsets, which is what makes an
instruction list editable: inserting or removing instructions shifts every
offset in the code object, but a label stays attached to the instruction it
points at.

Make one with `bc.new_label()`, append it to the `labels` list of the
instruction it should land on, and pass it as the argument of the jump. For a
target one past the end of the code, append it to `bc.end_labels` instead.
`bc.label_positions()` maps every label to the index in `instrs` it currently
resolves to.

```python
bc = Bytecode()
falsy = bc.new_label()

instrs = [Instr("LOAD_FAST", "x")]
if sys.version_info >= (3, 13):
    instrs.append(Instr("TO_BOOL"))  # 3.13+ POP_JUMP_IF_* only accepts a bool
instrs += [
    Instr("POP_JUMP_IF_FALSE", falsy),
    Instr("LOAD_CONST", "truthy"),
    Instr("RETURN_VALUE"),
    Instr("LOAD_CONST", "falsy"),
    Instr("RETURN_VALUE"),
]
instrs[-2].labels.append(falsy)
bc.instrs += instrs
```

Nothing here needs to know how far the jump reaches: the encoder picks the
relative or absolute form the opcode wants, and grows `EXTENDED_ARG` prefixes
to a fixed point when an oparg does not fit in a byte — which it has to iterate,
since growing one prefix moves every target after it.

### Transforming an existing code object

Instrumentation is the case the API is really shaped for: decode, splice, and
encode back.

```python
import sys

from spasm.bytecode import Bytecode, Instr, encode_name_arg

PY311 = sys.version_info >= (3, 11)


def f(a, b):
    return a * b


bc = Bytecode.from_code(f.__code__)

# The flag bit makes LOAD_GLOBAL push the NULL that the call sequence wants,
# in whichever order this version expects it; before 3.11 there is no NULL.
flag = {"flag": True} if PY311 else {}
trace = [
    Instr("LOAD_GLOBAL", encode_name_arg(bc, "LOAD_GLOBAL", "print", **flag)),
    Instr("LOAD_CONST", "called!"),
    Instr("CALL" if PY311 else "CALL_FUNCTION", 1),
    Instr("POP_TOP"),
]

# RESUME has to stay the first instruction of a 3.11+ code object.
at = 1 if PY311 else 0
for instr in trace:
    instr.lineno = bc.instrs[at].lineno
bc.instrs[at:at] = trace

f.__code__ = bc.to_code()
f(3, 4)  # prints "called!" and returns 12
```

Give inserted instructions a `lineno` explicitly. Nothing forces you to, but
the line table is what tracebacks and debuggers read, and an instrumented
function whose lines have drifted is unpleasant to debug.

### Exception table entries

From 3.11 on, exception handling is table-driven rather than done with block
instructions, and `bc.exc_entries` is that table. An `ExcEntry` is three labels
— the protected region's `start` and (exclusive) `stop`, and the `handler` —
plus the stack `depth` the handler is entered at and whether the interpreter
should push `lasti` before the exception.

```python
from spasm.bytecode import Bytecode, ExcEntry, Instr


def risky(x):
    return 1 / x


bc = Bytecode.from_code(risky.__code__)

start = bc.new_label()
handler = bc.new_label()
bc.instrs[1].labels.append(start)  # everything after RESUME is protected

recover = [
    Instr("POP_TOP"),  # the exception the interpreter pushed for us
    Instr("LOAD_CONST", None),
    Instr("RETURN_VALUE"),
]
recover[0].labels.append(handler)
for instr in recover:
    instr.lineno = bc.instrs[-1].lineno
bc.instrs += recover

# stop is exclusive, so the handler's own label doubles as the end of the
# region it handles.
bc.exc_entries.append(ExcEntry(start, handler, handler))

risky.__code__ = bc.to_code()
assert risky(0) is None
```

`depth` defaults to being inferred, which works whenever the depth at the start
of the protected region follows from normal control flow. Where it does not —
a handler reachable only through another handler's cleanup path, say —
`to_code()` raises instead of guessing, and you pass the depth yourself.

### What is checked, and what is not

`to_code()` computes `co_stacksize`, resolves labels, encodes the line and
exception tables and rejects opcodes the interpreter reserves for itself: the
`INSTRUMENTED_*` family, `ENTER_EXECUTOR`, `CACHE` and the pseudo-opcodes. That
last check exists because handing some of them to `PyCode_New` does not raise,
it crashes.

Beyond that, this is an assembly language: it will faithfully encode a stack
effect that does not balance, a `LOAD_FAST` reading a variable that was never
stored, or a jump into the middle of an instruction's inline caches, and the
interpreter will fault on it. Bytecode that is *valid* runs; bytecode that
merely assembles need not.

Finally, opcodes and calling conventions move between releases — `RESUME` and
`BINARY_OP` arrived in 3.11, `POP_JUMP_IF_FALSE` wants a real `bool` on the
stack from 3.13, the NULL a call needs is pushed in a different order in 3.13
than in 3.12 — so code written against this API targets an interpreter version
in a way that source-level `spasm` snippets partly hide. The `dis` module for
the version you are targeting is the reference.


## Architecture

`spasmlang` is two layers.

`spasm._core` is a C++ extension implementing the bytecode data model —
`Bytecode`, `Instr`, `Label`, `ExcEntry` — along with code object decoding and
encoding, line table and exception table handling, and stack depth
computation. It carries opcode tables generated at build time for the exact
interpreter it is compiled against (see `setup.py`), which is why there is one
wheel per Python minor version rather than a single abi3 wheel.

`spasm.bytecode` is a thin Python layer over it holding the parts that are
version-dependent bookkeeping rather than data structure: the `Compare` and
`BinaryOp` symbolic opargs and their per-version encodings, name-argument
packing for `LOAD_GLOBAL`/`LOAD_ATTR`, and `co_flags` inference. `spasm._asm`
builds on both and stays concerned with parsing and assembly; its `Assembly`
class is re-exported as `spasm.Assembly` (and driven, for the docstring-based
form, by the `spasm.asm` decorator) so nothing outside this package needs to
import the private module directly.

Stack depth (`co_stacksize`) is computed for you. Exception table entry depths
are inferred too, but only where that can be done exactly — see the note in
`spasm/bytecode.py` and `src/stackdepth.cpp`; `to_code()` raises rather than
emit a depth it cannot derive correctly.

This code was previously developed as a separate `bytecode-native` package and
has since been absorbed here.


## License

`spasmlang` is distributed under the terms of the
[MIT](https://spdx.org/licenses/MIT.html) license.


