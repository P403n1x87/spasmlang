#pragma once

#include "compat.h"
#include "instr.h"

#include <vector>
#include <cstdint>

// Decode the line/location table from a code object into a parallel vector of
// Location, one entry per logical instruction (EXTENDED_ARGs and CACHEs folded).
// `raw` and `nbytes` are the bytecode bytes needed to correlate code units with
// logical instructions (each linetable entry's `length` counts code units, which
// includes CACHE and EXTENDED_ARG words that are NOT separate logical instructions).
std::vector<Location> decode_linetable(PyCodeObject* co,
                                       const uint8_t* raw, Py_ssize_t nbytes);

// Encode a vector of Location (one per logical instruction) back into the
// format expected by the running interpreter.  Returns a new bytes object, or
// NULL on error.  The caller is responsible for DECREF.
PyObject* encode_linetable(const std::vector<InstrSlot>& slots,
                           int firstlineno);

#if !HAS_NEW_LINETABLE
// Helpers specific to the old lnotab (3.10) format, exposed for testing.
// Both operate on raw byte buffers.
std::vector<Location> decode_lnotab(const uint8_t* lnotab, Py_ssize_t len,
                                    int firstlineno,
                                    const uint8_t* raw, Py_ssize_t nbytes);
PyObject* encode_lnotab(const std::vector<InstrSlot>& slots, int firstlineno);
#endif
