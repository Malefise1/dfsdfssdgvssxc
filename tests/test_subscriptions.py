from dataclasses import dataclass

import pytest

from app.services.subscriptions import SubscriptionCountError, choose_random_items


@dataclass(frozen=True)
class Item:
    id: int


def test_random_selection_has_requested_unique_items():
    items = [Item(index) for index in range(1, 11)]
    selected = choose_random_items(items, 5)

    assert len(selected) == 5
    assert len({item.id for item in selected}) == 5
    assert set(selected).issubset(set(items))


@pytest.mark.parametrize("count", [0, -1, 4])
def test_random_selection_rejects_unavailable_count(count):
    with pytest.raises(SubscriptionCountError) as exc_info:
        choose_random_items([Item(1), Item(2), Item(3)], count)

    assert exc_info.value.available == 3
