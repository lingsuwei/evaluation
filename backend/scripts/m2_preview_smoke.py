#!/usr/bin/env python3
import json
import urllib.parse
import urllib.request

API = "http://127.0.0.1:3000/api"
TRACE_ID = "176d312356f286da958dc1961db0416e"
PROJECT = "cc-consume-2.0"
AGENT = "验收Agent-A-改名"

with urllib.request.urlopen(
    API + "/checkpoints/templates?agent_name=" + urllib.parse.quote(AGENT)
) as resp:
    templates = json.load(resp)

print(f"templates={len(templates)} trace={TRACE_ID} project={PROJECT}")
for name in ["发生过工具调用", "主 Agent 循环"]:
    tpl = next(x for x in templates if x["name"] == name)
    body = json.dumps(
        {"trace_id": TRACE_ID, "project": PROJECT, "source": "langfuse"}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{API}/checkpoints/templates/{tpl['id']}/preview",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as prev_resp:
        prev = json.load(prev_resp)
    result = (prev.get("results") or [{}])[0]
    print(
        name,
        "hit=",
        result.get("hit"),
        "matched=",
        result.get("matched_count"),
        "source=",
        prev.get("observation_source"),
    )
