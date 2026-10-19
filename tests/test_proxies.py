import pytest

from app.services.proxies import parse_proxy_file, parse_proxy_line


def test_proxy_format():
    proxy = parse_proxy_line("login:password@127.0.0.1:1080")
    assert proxy.login == "login"
    assert proxy.password == "password"
    assert proxy.host == "127.0.0.1"
    assert proxy.port == 1080


def test_invalid_proxy():
    with pytest.raises(ValueError):
        parse_proxy_line("127.0.0.1:1080")


def test_proxy_file_reports_line_errors():
    rows, errors = parse_proxy_file("a:b@host:80\nwrong\nc:d@host:1080")
    assert len(rows) == 2
    assert errors[0][0] == 2
