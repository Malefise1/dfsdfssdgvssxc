import pytest

from app.telegram.links import InvalidChannelLink, parse_channel_links, parse_public_channel_link


@pytest.mark.parametrize(
    ("raw", "username"),
    [
        ("@public_name", "public_name"),
        ("public_name", "public_name"),
        ("https://t.me/public_name", "public_name"),
        ("t.me/public_name/123", "public_name"),
    ],
)
def test_public_links(raw, username):
    assert parse_public_channel_link(raw).username == username


@pytest.mark.parametrize(
    "raw",
    [
        "https://t.me/+secret",
        "https://t.me/joinchat/xxx",
        "https://t.me/c/123/4",
        "https://example.com/a",
    ],
)
def test_private_or_foreign_links_rejected(raw):
    with pytest.raises(InvalidChannelLink):
        parse_public_channel_link(raw)


def test_multiple_links_deduplicated():
    links = parse_channel_links("@public_name\nhttps://t.me/public_name other_name")
    assert [item.username for item in links] == ["public_name", "other_name"]
