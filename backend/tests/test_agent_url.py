import os

import pytest

from utils.agent_url import resolve_agent_base_url


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://localhost:5055", "http://localhost:5055"),
        ("http://127.0.0.1:5055", "http://127.0.0.1:5055"),
        ("http://onto-agent:5055", "http://onto-agent:5055"),
    ],
)
def test_resolve_agent_base_url_on_host(monkeypatch, url, expected):
    monkeypatch.delenv("RUNNING_IN_DOCKER", raising=False)
    monkeypatch.setattr("utils.agent_url.is_running_in_docker", lambda: False)
    assert resolve_agent_base_url(url) == expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://localhost:5055", "http://host.docker.internal:5055"),
        ("http://127.0.0.1:5055", "http://host.docker.internal:5055"),
        ("http://onto-agent:5055", "http://onto-agent:5055"),
    ],
)
def test_resolve_agent_base_url_in_docker(monkeypatch, url, expected):
    monkeypatch.setenv("ONTO_AGENT_DOCKER_HOST", "host.docker.internal")
    monkeypatch.setattr("utils.agent_url.is_running_in_docker", lambda: True)
    assert resolve_agent_base_url(url) == expected


def test_resolve_agent_base_url_custom_docker_host(monkeypatch):
    monkeypatch.setenv("ONTO_AGENT_DOCKER_HOST", "172.17.0.1")
    monkeypatch.setattr("utils.agent_url.is_running_in_docker", lambda: True)
    assert resolve_agent_base_url("http://localhost:5055") == "http://172.17.0.1:5055"
