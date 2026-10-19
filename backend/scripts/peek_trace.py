#!/usr/bin/env python3
import json
import urllib.request

trace_id = "176d312356f286da958dc1961db0416e"
project = "cc-consume-2.0"
url = f"http://127.0.0.1:3000/api/trace/traces/{trace_id}?project={project}"
with urllib.request.urlopen(url, timeout=120) as resp:
    data = json.load(resp)
obs = data.get("observations") or data.get("trace", {}).get("observations") or []
print("obs count", len(obs))
names = sorted({str(o.get("name") or o.get("type") or "") for o in obs[:50]})
for n in names[:30]:
    print("-", n)
