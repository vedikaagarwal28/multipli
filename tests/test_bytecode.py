import pytest

from app.features.bytecode import is_eoa_code


@pytest.mark.parametrize("code", [
    "0x", "0x0", "",
    "0xef01005a7fc11397e9a8ad41bf10bf13f22b0a63f96f6d",  # live EIP-7702 delegation
    "0xEF0100" + "A" * 40,                               # checksummed/upper hex
    "  0x  ",
])
def test_eoa_code(code):
    assert is_eoa_code(code) is True


@pytest.mark.parametrize("code", [
    "0x60806040",
    "0xef",           # too short to be a delegation indicator
    "0xef0200" + "c" * 40,
])
def test_contract_code(code):
    assert is_eoa_code(code) is False
