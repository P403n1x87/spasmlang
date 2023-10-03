import pytest

from spasm.__main__ import SpasmUnmarshalError
from spasm.__main__ import spasm
from spasm.asm import SpasmParseError


def test_spasm_unmarshallable(tmp_path):
    source = tmp_path / "unmarshal.pya"

    source.write_text(
        """
            load_const          print
            store_global        $_print
            load_const          None
            return_value
        """
    )

    with pytest.raises(SpasmUnmarshalError):
        spasm(source)


def test_spasm_instr(tmp_path):
    source = tmp_path / "unmarshal.pya"

    source.write_text(
        """
            load_consts         print
            store_global        $_print
            load_const          None
            return_value
        """
    )

    with pytest.raises(SpasmParseError):
        spasm(source)
