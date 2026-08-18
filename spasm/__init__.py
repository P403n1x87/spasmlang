# SPDX-FileCopyrightText: 2023-present Gabriele N. Tornetta <phoenix1987@gmail.com>
#
# SPDX-License-Identifier: MIT

from spasm._asm import Assembly
from spasm.decorators import asm
from spasm.inliner import inline

__all__ = ["Assembly", "asm", "inline"]
