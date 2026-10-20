# src/dailywallpaper/net.py
"""Small network helpers shared by the sources and the prefetcher."""
from __future__ import annotations

import socket
from urllib.parse import urlparse

DEFAULT_PROBE_SECONDS = 0.5


def proxy_alive(proxy: str, timeout: float = DEFAULT_PROBE_SECONDS) -> bool:
    """True when something is accepting connections on the proxy's port.

    Pigeon/Clash can be running with its proxy switched off: requests then
    fails every host with ProxyError, including domestic ones like Bing that
    work fine on a direct connection. Probe first and fall back to direct.
    """
    text = (proxy or "").strip()
    if not text:
        return False
    parsed = urlparse(text if "://" in text else f"http://{text}")
    host = parsed.hostname
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
