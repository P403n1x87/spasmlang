# spasmlang

[![PyPI - Version](https://img.shields.io/pypi/v/spasmlang.svg)](https://pypi.org/project/spasmlang)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/spasmlang.svg)](https://pypi.org/project/spasmlang)


## Synopsis

`spasmlang` is a **s**imple **P**ython **as**se**m**bly **lang**uage. It is
essentially a high-level interface on top of the [bytecode][bytecode] package
that allows you to generate bytecode from a simple assembly-like syntax.

-----

**Table of Contents**

- [Installation](#installation)
- [Usage](#usage)
- [Examples](#examples)
- [License](#license)


## Installation

```console
pip install spasmlang
```


## Usage

The `spasmlang` package provides a single class, `Assembly`, that allows you to
generate bytecode from a simple assembly-like syntax. See the [examples](#examples)
below for a taste of its API.

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


## License

`spasmlang` is distributed under the terms of the
[MIT](https://spdx.org/licenses/MIT.html) license.


[bytecode]: https://github.com/MatthieuDartiailh/bytecode
