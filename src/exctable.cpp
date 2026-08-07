#include "exctable.h"

#if HAS_EXCEPTION_TABLE

// Exception table varint format (3.11+) — BIG-ENDIAN, MSB first:
//   Bit 7 (0x80): marks the first byte of the 'start' field (entry boundary marker)
//   Bit 6 (0x40): continuation — more bytes follow for this value
//   Bits 5-0: 6 data bits, most-significant group first
//
// All raw values are instruction WORD offsets (× 2 = byte offset).
// Reference: CPython Python/assemble.c assemble_emit_exception_table_item()

static unsigned int read_varint(const uint8_t*& p, const uint8_t* end)
{
    // Bit 7 is ignored (it's an entry-start marker on the 'start' field only).
    // Bit 6 = continuation. Groups arrive MSB-first.
    uint8_t b = *p++;
    unsigned int val = b & 0x3F;       // mask off bit7 (msb marker) and bit6 (cont)
    while (b & 0x40) {                  // bit6 = continuation
        b = *p++;
        val = (val << 6) | (b & 0x3F); // shift previous bits up, add new 6 bits
    }
    return val;
}

std::vector<ExcEntry> decode_exctable(PyCodeObject* co)
{
    std::vector<ExcEntry> result;

    const uint8_t* p   = reinterpret_cast<const uint8_t*>(
                            PyBytes_AS_STRING(co->co_exceptiontable));
    const uint8_t* end = p + PyBytes_GET_SIZE(co->co_exceptiontable);

    while (p < end) {
        unsigned int start   = read_varint(p, end);
        unsigned int size    = read_varint(p, end);
        unsigned int handler = read_varint(p, end);
        unsigned int dl      = read_varint(p, end);

        ExcEntry e;
        e.start_offset   = start             * 2;
        e.stop_offset    = (start + size)    * 2;
        e.handler_offset = handler           * 2;
        e.depth          = static_cast<int>(dl >> 1);
        e.lasti          = (dl & 1) != 0;
        result.push_back(e);
    }
    return result;
}

// Write a varint value in big-endian (MSB-first) format.
// msb_flag is ORed into the first byte (0x80 for the 'start' field, 0 otherwise).
static void write_varint(std::vector<uint8_t>& out, unsigned int val,
                         uint8_t msb_flag = 0)
{
    // Determine number of 6-bit groups needed.
    unsigned int tmp = val;
    int n = 1;
    while (tmp >>= 6) ++n;

    // Emit MSB group first; all but the last byte get the continuation bit.
    for (int i = n - 1; i >= 0; --i) {
        uint8_t b = static_cast<uint8_t>((val >> (i * 6)) & 0x3F);
        if (i > 0)    b |= 0x40;   // continuation
        if (i == n-1) b |= msb_flag; // entry-start marker on first byte of 'start'
        out.push_back(b);
    }
}

PyObject* encode_exctable(const std::vector<ExcEntry>& entries)
{
    std::vector<uint8_t> out;
    out.reserve(entries.size() * 8);

    for (const auto& e : entries) {
        unsigned int start   = e.start_offset                          / 2;
        unsigned int size    = (e.stop_offset - e.start_offset)       / 2;
        unsigned int handler = e.handler_offset                        / 2;
        unsigned int dl      = static_cast<unsigned int>(e.depth) * 2
                               + (e.lasti ? 1u : 0u);

        write_varint(out, start,   0x80);  // bit7 marks start of entry
        write_varint(out, size);
        write_varint(out, handler);
        write_varint(out, dl);
    }

    return PyBytes_FromStringAndSize(
        reinterpret_cast<const char*>(out.data()),
        static_cast<Py_ssize_t>(out.size()));
}

#endif  // HAS_EXCEPTION_TABLE
