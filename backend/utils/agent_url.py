"""Resolve agent base URLs for Docker vs host networking."""
from __future__ import annotations

import os
from urllib.parse import urlparse, urlunparse

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_running_in_docker() -> bool:
    return os.path.exists("/.dockerenv") or os.getenv("RUNNING_IN_DOCKER") == "1"


def resolve_agent_base_url(url: str) -> str:
    """
    When onto-eval backend runs in Docker, localhost points at the container itself.
    Rewrite localhost/127.0.0.1 to host.docker.internal so onto-agent on the host is reachable.
    """
    raw = (url or "").strip()
    if not raw:
        return raw

    override = os.getenv("ONTO_AGENT_DOCKER_HOST", "host.docker.internal").strip()
    if not override or not is_running_in_docker():
        return raw.rstrip("/")

    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host not in _LOCAL_HOSTS:
        return raw.rstrip("/")

    port = parsed.port
    if port is not None:
        new_netloc = f"{override}:{port}"
    else:
        new_netloc = override

    resolved = urlunparse(parsed._replace(netloc=new_netloc)).rstrip("/")
    return resolved
