import pytest

from app.services.accounts import normalize_phone, smallest_free_account_number


def test_phone_normalization():
    assert normalize_phone("+372 555-55-555") == "+37255555555"


def test_invalid_phone():
    with pytest.raises(ValueError):
        normalize_phone("55555")


@pytest.mark.parametrize(
    ("used", "expected"),
    [
        ([], 1),
        ([1, 2, 3], 4),
        ([4], 1),
        ([1, 2, 4, 5], 3),
    ],
)
def test_smallest_free_account_number(used, expected):
    assert smallest_free_account_number(used) == expected
