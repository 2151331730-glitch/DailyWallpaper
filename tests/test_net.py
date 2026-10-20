# tests/test_net.py
import socket
from types import SimpleNamespace

from dailywallpaper.net import proxy_alive


def _cfg(proxy=""):
    return SimpleNamespace(sources=SimpleNamespace(proxy=proxy))


def _free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _listening_server():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    return server, server.getsockname()[1]


def test_a_listening_proxy_is_detected():
    for template in ("http://127.0.0.1:{port}", "127.0.0.1:{port}"):
        server, port = _listening_server()
        try:
            assert proxy_alive(template.format(port=port), timeout=1) is True
        finally:
            server.close()


def test_a_closed_port_is_not_alive():
    assert proxy_alive(f"http://127.0.0.1:{_free_port()}") is False


def test_empty_or_hostless_values_are_not_alive():
    assert proxy_alive("") is False
    assert proxy_alive("   ") is False
    assert proxy_alive("http://") is False


def test_session_ignores_the_environment_proxy(monkeypatch):
    from dailywallpaper.app import configure_session

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7892")

    session = configure_session(_cfg())

    assert session.trust_env is False
    assert session.proxies == {}


def test_session_uses_a_live_configured_proxy(monkeypatch):
    from dailywallpaper import app

    server, port = _listening_server()
    try:
        proxy = f"http://127.0.0.1:{port}"
        session = app.configure_session(_cfg(proxy))
        assert session.trust_env is False
        assert session.proxies["https"] == proxy
    finally:
        server.close()


def test_session_goes_direct_when_the_configured_proxy_is_dead():
    from dailywallpaper.app import configure_session

    session = configure_session(_cfg(f"http://127.0.0.1:{_free_port()}"))

    assert session.trust_env is False
    assert session.proxies == {}
