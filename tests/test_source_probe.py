import pytest

from okx_btc_pas.source_probe import parse_cbr_xml


def test_cbr_xml_fields():
    rows = parse_cbr_xml(
        b"<ValCurs ID='R01235'><Record Date='11.01.2018' Id='R01235'>"
        b"<Nominal>1</Nominal><Value>56,0000</Value></Record></ValCurs>"
    )
    assert rows[0]["Date"] == "11.01.2018"
    assert rows[0]["Value"] == "56,0000"


def test_wrong_currency_is_rejected():
    with pytest.raises(ValueError):
        parse_cbr_xml(b"<ValCurs ID='R01239'/>")
