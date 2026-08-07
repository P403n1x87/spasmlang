#include "linetable.h"
#include "compat.h"
#include "cache_sizes_gen.h"
#include <algorithm>

#include <cassert>
#include <cstring>

// ════════════════════════════════════════════════════════════════════════════
// 3.10 — lnotab (legacy pairs format)
// Format: sequence of (bytecode_delta, line_delta) byte pairs.
// line_delta == 0 means "same line"; 255 means "+255" (may chain).
// ════════════════════════════════════════════════════════════════════════════
#if !HAS_NEW_LINETABLE

std::vector<Location> decode_lnotab(const uint8_t* lnotab, Py_ssize_t len,
                                    int firstlineno,
                                    const uint8_t* raw, Py_ssize_t nbytes)
{
    // Offsets in the table are byte offsets, which don't divide down to
    // logical instruction indices: from_code folds each EXTENDED_ARG word into
    // the instruction it prefixes. Map the two explicitly, the way the 3.11+
    // decoder does for CACHE words. An offset landing on an EXTENDED_ARG maps
    // to the instruction it belongs to.
    Py_ssize_t n_words = nbytes / INSTR_BYTES;
    std::vector<int> index_at(static_cast<size_t>(n_words), 0);
    int n_instrs = 0;
    for (Py_ssize_t w = 0; w < n_words; ++w) {
        index_at[static_cast<size_t>(w)] = n_instrs;
        if (raw[w * INSTR_BYTES] != EXTENDED_ARG) ++n_instrs;
    }

    std::vector<Location> locs(n_instrs, Location{firstlineno});
    std::vector<bool> mentioned(n_instrs, false);

    int lineno  = firstlineno;
    int byte_off = 0;
    // Each pair (bdelta, ldelta) means: the line advances by ldelta, and
    // that new line applies starting at the CURRENT byte_off (before this
    // entry's bdelta is added) — bdelta only advances the starting point
    // for the *next* entry. Must index locs[] before advancing byte_off.
    for (Py_ssize_t i = 0; i + 1 < len; i += 2) {
        int bdelta = lnotab[i];
        int ldelta = static_cast<int8_t>(lnotab[i + 1]); // signed since 3.6

        int entry_line;
        if (ldelta == -128) {
            // Not a delta: 3.10 marks a range with no line number this way,
            // and leaves the running line where it was (see advance() in
            // 3.10's codeobject.c). Negative is how Location spells "no
            // line", the same as the NONE entry in the 3.11+ format.
            entry_line = -1;
        } else {
            lineno += ldelta;
            entry_line = lineno;
        }

        Py_ssize_t word = byte_off / INSTR_BYTES;
        if (word < n_words) {
            int instr_idx = index_at[static_cast<size_t>(word)];
            locs[instr_idx].lineno = entry_line;
            mentioned[instr_idx]   = true;
        }
        byte_off += bdelta;
    }

    // Forward-fill: an entry covers a range of instructions but only the one
    // at its start gets mentioned above.
    for (int i = 1; i < n_instrs; ++i)
        if (!mentioned[i])
            locs[i].lineno = locs[i-1].lineno;

    return locs;
}

PyObject* encode_lnotab(const std::vector<InstrSlot>& slots, int firstlineno)
{
    // A (bdelta, ldelta) pair means: the line advances by ldelta, and that
    // new line applies starting at the CURRENT byte offset; bdelta is the
    // number of bytes the new line SPANS (i.e. the gap to the *next*
    // breakpoint, not the gap from the previous one) — see decode_lnotab
    // above for the matching read side. So we first collect breakpoints
    // (offset, line) wherever the line changes, then derive each entry's
    // bdelta from the *following* breakpoint's offset.
    struct Breakpoint { uint32_t offset; int line; };
    std::vector<Breakpoint> bps;

    int cur_line = firstlineno;
    for (const auto& slot : slots) {
        // A negative lineno means "no line", which is a range of its own
        // rather than a continuation of the previous one.
        int line = slot.instr.loc.lineno;
        if (line != cur_line) {
            bps.push_back({slot.offset, line});
            cur_line = line;
        }
    }

    // The table describes the code from offset 0 onwards, so it needs an entry
    // starting there even when the first instructions sit on firstlineno and
    // so produce no breakpoint of their own. Without it the run at firstlineno
    // is dropped and every later entry slides down to cover it.
    if (!slots.empty() && (bps.empty() || bps.front().offset != 0))
        bps.insert(bps.begin(), {0, firstlineno});

    uint32_t total_bytes = slots.empty()
        ? 0
        : (slots.back().offset + INSTR_BYTES);

    std::vector<uint8_t> out;
    out.reserve(bps.size() * 2);

    int prev_line = firstlineno;
    for (size_t k = 0; k < bps.size(); ++k) {
        uint32_t start  = bps[k].offset;
        uint32_t end    = (k + 1 < bps.size()) ? bps[k + 1].offset : total_bytes;
        uint32_t bdelta = end - start;

        // -128 is the no-line marker, not a delta, so it leaves prev_line
        // alone — the next real line is relative to the last real one.
        const bool no_line = bps[k].line < 0;
        int ldelta = -128;
        if (!no_line) {
            ldelta = bps[k].line - prev_line;
            prev_line = bps[k].line;

            // ldelta out of range: chain (0, chunk) entries first — bdelta=0
            // means "no byte span yet", just further adjusts the line before
            // the real bdelta-carrying entry below. The negative chunk is
            // -127 rather than -128, since a real delta that happened to be
            // -128 would otherwise read back as a no-line marker.
            while (ldelta > 127) {
                out.push_back(0);
                out.push_back(127);
                ldelta -= 127;
            }
            while (ldelta < -127) {
                out.push_back(0);
                out.push_back(static_cast<uint8_t>(-127));
                ldelta += 127;
            }
        }

        // Emit the real (bdelta, ldelta) entry, then chain continuation
        // entries for any bdelta remainder beyond 255. Each entry carries its
        // own line, so a continuation of a no-line range has to repeat the
        // marker rather than say "same line as before".
        const uint8_t cont_ldelta = no_line ? static_cast<uint8_t>(-128) : 0;
        out.push_back(static_cast<uint8_t>(bdelta > 255 ? 255 : bdelta));
        out.push_back(static_cast<uint8_t>(ldelta));
        bdelta = (bdelta > 255) ? bdelta - 255 : 0;
        while (bdelta > 0) {
            out.push_back(static_cast<uint8_t>(bdelta > 255 ? 255 : bdelta));
            out.push_back(cont_ldelta);
            bdelta = (bdelta > 255) ? bdelta - 255 : 0;
        }
    }

    return PyBytes_FromStringAndSize(
        reinterpret_cast<const char*>(out.data()),
        static_cast<Py_ssize_t>(out.size()));
}

std::vector<Location> decode_linetable(PyCodeObject* co,
                                       const uint8_t* raw, Py_ssize_t nbytes)
{
    // In 3.10 the internal field is co_linetable (renamed from co_lnotab),
    // but still encodes the old lnotab byte-pair format. There are no CACHE
    // words to skip at this version, but EXTENDED_ARG ones still have to be,
    // which is why the raw bytecode is needed.
    PyObject* lnotab = co->co_linetable;
    return decode_lnotab(
        reinterpret_cast<const uint8_t*>(PyBytes_AS_STRING(lnotab)),
        PyBytes_GET_SIZE(lnotab),
        co->co_firstlineno,
        raw, nbytes);
}

PyObject* encode_linetable(const std::vector<InstrSlot>& slots, int firstlineno)
{
    return encode_lnotab(slots, firstlineno);
}

// ════════════════════════════════════════════════════════════════════════════
// 3.11+ — new location-entry format
// See Include/cpython/code.h (PY_CODE_LOCATION_INFO_*) and
// InternalDocs/code_objects.md for the full spec.
//
// Each entry header byte encodes:
//   bits 7-3 : code (5 bits) — determines entry type
//   bits 2-0 : length-1 (3 bits) — covers (length) instructions
//
// Code values (see PY_CODE_LOCATION_INFO_* enum in cpython/code.h):
//   0–9   SHORT0+n  column in [0..79], short form
//   10    ONE_LINE0 one-line, col + end_col in varint
//   11    ONE_LINE1 one-line, line_delta=1
//   12    ONE_LINE2 one-line, line_delta=2
//   13    NO_COLUMNS  line number only
//   14    LONG      full: line_delta + col + end_col as varints
//   15    NONE      no location info
// ════════════════════════════════════════════════════════════════════════════
#else  // HAS_NEW_LINETABLE

// ── varint helpers ───────────────────────────────────────────────────────────

static int read_varint(const uint8_t*& p, const uint8_t* end)
{
    int val = 0, shift = 0;
    while (p < end) {
        uint8_t b = *p++;
        val |= (b & 0x3F) << shift;
        shift += 6;
        if (!(b & 0x40)) break;
    }
    return val;
}

static int read_signed_varint(const uint8_t*& p, const uint8_t* end)
{
    int uval = read_varint(p, end);
    // sign bit is the LSB
    return (uval & 1) ? -(uval >> 1) : (uval >> 1);
}

// ── decode ───────────────────────────────────────────────────────────────────
//
// Each linetable entry's `length` field counts CODE UNITS (2-byte words),
// including CACHE and EXTENDED_ARG words.  We must advance instr_idx only
// for REAL instructions (non-CACHE, non-EXTENDED_ARG).  The raw bytecode is
// required to classify each code unit correctly.

std::vector<Location> decode_linetable(PyCodeObject* co,
                                       const uint8_t* raw, Py_ssize_t nbytes)
{
    // Build a parallel list of "is this code unit a logical instruction?"
    // flags, using the same CACHE-skip / EXTENDED_ARG-fold logic as from_code().
    // true  = logical instruction (instr_idx should advance here)
    // false = CACHE or EXTENDED_ARG word (skip for instr_idx)
    Py_ssize_t n_words = nbytes / INSTR_BYTES;
    std::vector<bool> is_logical(static_cast<size_t>(n_words), false);
    {
        int skip_cache = 0;
        for (Py_ssize_t i = 0; i < nbytes; i += INSTR_BYTES) {
            uint8_t op = raw[i];
#if HAS_CACHE_ENTRIES
            if (op == 0) {
                if (skip_cache > 0) { --skip_cache; }
                continue;  // CACHE: not a logical instruction
            }
#endif
            if (op == EXTENDED_ARG) {
                continue;  // EXTENDED_ARG: not a logical instruction
            }
            is_logical[static_cast<size_t>(i / INSTR_BYTES)] = true;
#if HAS_CACHE_ENTRIES
            skip_cache = instr_cache_size(op);
#endif
        }
    }

    int n_instrs = static_cast<int>(
        std::count(is_logical.begin(), is_logical.end(), true));

    std::vector<Location> locs(static_cast<size_t>(n_instrs), Location{});

    const uint8_t* p   = reinterpret_cast<const uint8_t*>(
                            PyBytes_AS_STRING(co->co_linetable));
    const uint8_t* end = p + PyBytes_GET_SIZE(co->co_linetable);
    int firstlineno    = co->co_firstlineno;

    int instr_idx  = 0;
    int unit_idx   = 0;   // current code-unit index
    int lineno     = firstlineno;

    while (p < end && instr_idx < n_instrs) {
        uint8_t hdr   = *p++;
        // Bit 7 of the header byte is a marker (used by CPython's scanner
        // to locate entry starts quickly); bits 6-3 hold the 4-bit code and
        // bits 2-0 hold (length-1).  Mask to 4 bits to ignore bit 7.
        int code      = (hdr >> 3) & 0x0F;
        int length    = (hdr & 0x07) + 1;   // number of CODE UNITS covered

        Location loc;
        loc.lineno     = lineno;
        loc.end_lineno = lineno;
        loc.col_offset = -1;
        loc.end_col    = -1;

        switch (code) {
        case 15: // NONE
            loc.lineno = loc.end_lineno = -1;
            break;

        case 14: { // LONG
            int line_delta = read_signed_varint(p, end);
            lineno += line_delta;
            int end_line_delta = read_varint(p, end);
            int col  = read_varint(p, end) - 1;
            int ecol = read_varint(p, end) - 1;
            loc.lineno     = lineno;
            loc.end_lineno = lineno + end_line_delta;
            loc.col_offset = col;
            loc.end_col    = ecol;
            break;
        }

        case 13: { // NO_COLUMNS
            int line_delta = read_signed_varint(p, end);
            lineno += line_delta;
            loc.lineno = loc.end_lineno = lineno;
            break;
        }

        case 12: case 11: case 10: { // ONE_LINE0/1/2
            // CPython format: line_delta = code - 10 (implicit, not stored).
            // col and end_col are raw single bytes (no varint, no +1 bias).
            int line_delta = code - 10;
            lineno += line_delta;
            loc.lineno     = lineno;
            loc.end_lineno = lineno;
            loc.col_offset = (p < end) ? static_cast<int>(static_cast<uint8_t>(*p++)) : -1;
            loc.end_col    = (p < end) ? static_cast<int>(static_cast<uint8_t>(*p++)) : -1;
            break;
        }

        default: { // SHORT0 (0–9)
            uint8_t b      = *p++;
            int col_group  = code;
            int col_low    = (b >> 4) & 0x0F;
            int col        = col_group * 8 + col_low;
            int end_col    = col + (b & 0x0F);
            loc.col_offset = col;
            loc.end_col    = end_col;
            loc.lineno = loc.end_lineno = lineno;
            break;
        }
        }

        // Assign this location to each LOGICAL instruction within the `length`
        // code units covered by this entry.
        for (int i = 0; i < length && instr_idx < n_instrs; ++i) {
            size_t wi = static_cast<size_t>(unit_idx + i);
            if (wi < is_logical.size() && is_logical[wi]) {
                locs[static_cast<size_t>(instr_idx++)] = loc;
            }
            // CACHE / EXTENDED_ARG words: skip without advancing instr_idx
        }
        unit_idx += length;
    }

    return locs;
}

// ── varint write helpers ─────────────────────────────────────────────────────

static constexpr uint8_t MSB = 0x80;

static void write_varint(std::vector<uint8_t>& out, unsigned int val)
{
    while (val >= 0x40) {
        out.push_back(static_cast<uint8_t>((val & 0x3F) | 0x40));
        val >>= 6;
    }
    out.push_back(static_cast<uint8_t>(val));
}

static void write_signed_varint(std::vector<uint8_t>& out, int val)
{
    write_varint(out, static_cast<unsigned int>(val < 0 ? (-val << 1) | 1 : val << 1));
}

// ── encode ───────────────────────────────────────────────────────────────────

PyObject* encode_linetable(const std::vector<InstrSlot>& slots, int firstlineno)
{
    std::vector<uint8_t> out;
    out.reserve(slots.size() * 2);

    int cur_lineno = firstlineno;
    size_t i = 0;
    const size_t n = slots.size();

    while (i < n) {
        const Location& loc = slots[i].instr.loc;

        // Count how many consecutive instructions share this location.
        size_t run = 1;
        while (i + run < n && run < 8) {
            const Location& next = slots[i + run].instr.loc;
            if (next.lineno     != loc.lineno     ||
                next.end_lineno != loc.end_lineno  ||
                next.col_offset != loc.col_offset  ||
                next.end_col    != loc.end_col)
                break;
            ++run;
        }
        uint8_t length_bits = static_cast<uint8_t>(run - 1); // 0–7

        // Bit 7 (MSB) of the header byte is a mandatory entry-boundary marker
        // used by CPython's bounds scanner (advance()/retreat() in
        // codeobject.c) to find the start of the next/previous entry without
        // decoding the variable-length body. It must be set on every header
        // byte we emit, or PyCode_Addr2Line()/tb_lineno silently desyncs.
        if (loc.lineno < 0) {
            // NONE
            out.push_back(static_cast<uint8_t>(MSB | (15 << 3) | length_bits));
        }
        else if (loc.col_offset < 0) {
            // NO_COLUMNS
            out.push_back(static_cast<uint8_t>(MSB | (13 << 3) | length_bits));
            write_signed_varint(out, loc.lineno - cur_lineno);
            cur_lineno = loc.lineno;
        }
        else if (loc.lineno == loc.end_lineno) {
            int line_delta = loc.lineno - cur_lineno;
            int col  = loc.col_offset;
            int ecol = loc.end_col;
            int col_group = col >> 3;
            int col_low   = col & 7;
            // SHORT form: column < 80, ecol-col < 16 (see write_location_info_short_form).
            if (line_delta == 0 &&
                col < 80 &&
                ecol - col >= 0 && ecol - col < 16)
            {
                out.push_back(static_cast<uint8_t>(MSB | (col_group << 3) | length_bits));
                out.push_back(static_cast<uint8_t>((col_low << 4) | (ecol - col)));
            }
            else if (line_delta >= 0 && line_delta <= 2 &&
                     col >= 0 && col < 128 && ecol >= 0 && ecol < 128) {
                // ONE_LINE 0/1/2: header + col byte + end_col byte.
                // CPython format: delta is implicit in the code (code-10), NOT stored.
                // col and end_col are raw single bytes (no varint, no +1 bias).
                int code = 10 + line_delta;
                out.push_back(static_cast<uint8_t>(MSB | (code << 3) | length_bits));
                out.push_back(static_cast<uint8_t>(col));
                out.push_back(static_cast<uint8_t>(ecol));
                cur_lineno = loc.lineno;
            }
            else {
                // LONG
                out.push_back(static_cast<uint8_t>(MSB | (14 << 3) | length_bits));
                write_signed_varint(out, line_delta);
                write_varint(out, 0); // end_line_delta
                write_varint(out, static_cast<unsigned>(col  + 1));
                write_varint(out, static_cast<unsigned>(ecol + 1));
                cur_lineno = loc.lineno;
            }
        }
        else {
            // LONG (multi-line)
            out.push_back(static_cast<uint8_t>(MSB | (14 << 3) | length_bits));
            write_signed_varint(out, loc.lineno - cur_lineno);
            write_varint(out, static_cast<unsigned>(loc.end_lineno - loc.lineno));
            write_varint(out, static_cast<unsigned>(loc.col_offset + 1));
            write_varint(out, static_cast<unsigned>(loc.end_col    + 1));
            cur_lineno = loc.lineno;
        }

        i += run;
    }

    return PyBytes_FromStringAndSize(
        reinterpret_cast<const char*>(out.data()),
        static_cast<Py_ssize_t>(out.size()));
}

#endif  // HAS_NEW_LINETABLE
