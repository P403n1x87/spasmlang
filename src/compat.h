#pragma once

#include <Python.h>
#include <opcode.h>

// ── Version sentinels ────────────────────────────────────────────────────────
#define PY_310 0x030a0000
#define PY_311 0x030b0000
#define PY_312 0x030c0000
#define PY_313 0x030d0000
#define PY_314 0x030e0000

// ── Jump-arg encoding ────────────────────────────────────────────────────────
// Since WORDCODE (3.6+), all jump args are *word* offsets (byte_offset / 2).
// This applies across every version we support (3.10–3.14).
#define ARG_TO_BYTE_OFFSET(arg)  ((arg) * 2)
#define BYTE_OFFSET_TO_ARG(off)  ((off) / 2)

// ── Instruction word size (always 2 bytes since 3.6 WORDCODE) ───────────────
#define INSTR_BYTES 2

// ── co_code access ───────────────────────────────────────────────────────────
// 3.11 stores adaptive copy in _co_code_adaptive; public co_code is rebuilt.
// 3.12+ removed the public bytes field; use PyCode_GetCode() instead.
#if PY_VERSION_HEX >= PY_311
#  define GET_CODE_BYTES(co) PyCode_GetCode(co)   // returns new ref, may be NULL
#else
#  define GET_CODE_BYTES(co) (Py_INCREF((co)->co_code), (co)->co_code)
#endif

// ── Exception table ──────────────────────────────────────────────────────────
#if PY_VERSION_HEX >= PY_311
#  define HAS_EXCEPTION_TABLE 1
#else
#  define HAS_EXCEPTION_TABLE 0
#endif

// ── Line-number table format ─────────────────────────────────────────────────
// 3.10 : lnotab (legacy pairs format) accessed via co_lnotab
// 3.11+: co_linetable with the new location-entry format
#if PY_VERSION_HEX >= PY_311
#  define HAS_NEW_LINETABLE 1
#else
#  define HAS_NEW_LINETABLE 0
#endif

// ── RESUME instruction (mandatory first instruction since 3.11) ──────────────
#if PY_VERSION_HEX >= PY_311
#  define HAS_RESUME_INSTR 1
#else
#  define HAS_RESUME_INSTR 0
#endif

// ── co_varnames / co_cellvars / co_freevars ───────────────────────────────────
// These became opaque (private _co_* fields) in 3.13; use the public accessors.
// The accessors exist since 3.11, so use them unconditionally there.
#if PY_VERSION_HEX >= PY_311
#  define GET_CO_VARNAMES(co) PyCode_GetVarnames(co)   // new ref
#  define GET_CO_CELLVARS(co) PyCode_GetCellvars(co)   // new ref
#  define GET_CO_FREEVARS(co) PyCode_GetFreevars(co)   // new ref
#else
#  define GET_CO_VARNAMES(co) (Py_INCREF((co)->co_varnames), (co)->co_varnames)
#  define GET_CO_CELLVARS(co) (Py_INCREF((co)->co_cellvars), (co)->co_cellvars)
#  define GET_CO_FREEVARS(co) (Py_INCREF((co)->co_freevars), (co)->co_freevars)
#endif

// ── Code object constructor ───────────────────────────────────────────────────
// PyCode_NewWithPosOnlyArgs deprecated in 3.12; use PyUnstable_Code_NewWithPosOnlyArgs.
#if PY_VERSION_HEX >= PY_312
#  define PY_CODE_NEW_FN PyUnstable_Code_NewWithPosOnlyArgs
#else
#  define PY_CODE_NEW_FN PyCode_NewWithPosOnlyArgs
#endif
