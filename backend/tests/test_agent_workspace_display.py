"""Agent 工作空间展示解析单测。"""
from __future__ import annotations

import pytest

from database import Agent
from services.agent_workspace_display import (
    resolve_workspace_display,
    workspace_id_from_agent,
    workspace_name_from_agent,
)

pytestmark = pytest.mark.p3


def test_workspace_id_from_inference_config():
    ag = Agent(
        id="a1",
        name="n",
        inference_config='{"workspace_id":"ws-abc","auth":{"api_token":"x"},"base_url":"http://h"}',
    )
    assert workspace_id_from_agent(ag) == "ws-abc"


def test_resolve_workspace_display_uses_name_map():
    ag = Agent(id="a1", name="n", inference_config='{"workspace_id":"ws-1"}')
    assert resolve_workspace_display(ag, {"ws-1": "默认工作空间"}) == "默认工作空间"


def test_resolve_workspace_display_prefers_stored_name():
    ag = Agent(
        id="a1",
        name="n",
        inference_config=(
            '{"workspace_id":"zcproject","workspace_name":"ZC海域本体"}'
        ),
    )
    assert workspace_name_from_agent(ag) == "ZC海域本体"
    assert (
        resolve_workspace_display(ag, {"zcproject": "zc project"})
        == "ZC海域本体"
    )
