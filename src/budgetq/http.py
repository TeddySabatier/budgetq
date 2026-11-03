"""Minimal HTTPS GET helper."""

from __future__ import annotations

import json
import ssl
import urllib.request

_context: ssl.SSLContext | None = None


def tls_context() -> ssl.SSLContext:
    """A verifying TLS context that ignores SSLKEYLOGFILE.

    ssl.create_default_context() honours SSLKEYLOGFILE; some antivirus products
    (e.g. Avast) point it at a named pipe, which crashes several Python builds
    on Windows with "OPENSSL_Uplink ... no OPENSSL_Applink".
    """
    global _context
    if _context is None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_default_certs()  # OS trust store, including inspection proxies' roots
        _context = ctx
    return _context


def get_json(url: str, headers: dict[str, str], timeout: float) -> dict:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout, context=tls_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))
