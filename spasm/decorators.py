"""Writing spasm assembly directly inside a Python source file.

:func:`asm` replaces a decorated function's body with the assembly written in
its own docstring, compiled through the same :class:`spasm.Assembly` the
``spasm`` CLI and file-based ``.pya`` sources already use::

    import spasm

    @spasm.asm(retval=42)
    def answer():
        \"\"\"
        load_const {retval}
        return_value
        \"\"\"

The decorator's own keyword arguments are assembler bind-args (``{name}``
placeholders in the docstring, see :meth:`Assembly.compile`), resolved once
at decoration time — not the function's runtime parameters. Those come from
the function's own signature, same as any other ``def``, and are reached
from the assembly with ``load_fast``/``store_fast`` exactly as in a nested
``code`` block in a ``.pya`` file.
"""

import typing as t

from spasm._asm import Assembly
from spasm.bytecode import CO_VARARGS
from spasm.bytecode import CO_VARKEYWORDS

__all__ = ["asm"]

_F = t.TypeVar("_F", bound=t.Callable[..., t.Any])


def asm(**bind_args: t.Any) -> t.Callable[[_F], _F]:
    """Compile a decorated function's docstring as spasm assembly.

    ``bind_args`` are compile-time bind-args substituted into ``{name}``
    placeholders in the docstring before it's assembled — not the function's
    runtime parameters, which are taken from ``func.__code__`` rather than a
    textual ``code NAME(args)`` header, since the decorator already knows
    them. ``*args``/``**kwargs``, keyword-only arguments and closures
    (cellvars/freevars) aren't supported: write those with the nested
    ``code`` block syntax in a ``.pya`` file instead.
    """

    def decorator(func: _F) -> _F:
        code = func.__code__
        qualname = func.__qualname__

        if func.__doc__ is None:
            msg = f"{qualname}: asm requires a docstring containing the assembly"
            raise ValueError(msg)

        if code.co_flags & (CO_VARARGS | CO_VARKEYWORDS):
            msg = f"{qualname}: asm does not support *args or **kwargs"
            raise ValueError(msg)

        if code.co_kwonlyargcount:
            msg = f"{qualname}: asm does not support keyword-only arguments"
            raise ValueError(msg)

        if code.co_freevars or code.co_cellvars:
            msg = f"{qualname}: asm does not support closures"
            raise ValueError(msg)

        assembly = Assembly(
            name=qualname,
            filename=code.co_filename,
            lineno=code.co_firstlineno,
            is_function=True,
            argnames=list(code.co_varnames[: code.co_argcount]),
        )
        assembly.parse(func.__doc__)

        func.__code__ = assembly.compile(bind_args, lineno=code.co_firstlineno)  # type: ignore[misc]
        func.__doc__ = None

        return func

    return decorator
