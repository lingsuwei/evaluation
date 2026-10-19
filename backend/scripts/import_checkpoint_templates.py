#!/usr/bin/env python3
"""
从 JSON 批量导入 onto-eval 过程点模板（M2 联调）。

示例：
  cd backend
  python scripts/import_checkpoint_templates.py \\
    --json ../docs/M2-过程点配置-onto-agent识别.json \\
    --agent-name "我的Agent"

  python scripts/import_checkpoint_templates.py \\
    --json ../docs/M2-过程点-skill扫描结果.json \\
    --dry-run

JSON 格式：顶层 `templates` 数组，或扫描脚本输出的完整 payload（含 templates 字段）。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple


def _load_templates(path: Path) -> List[Dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        templates = raw.get("templates")
        if isinstance(templates, list):
            return templates
    raise ValueError("JSON 需为 templates 数组，或含 templates 字段的对象")


def _normalize_template(
    item: Dict[str, Any],
    *,
    agent_name: str | None,
    replace_agent: bool,
) -> Dict[str, Any]:
    out = {k: v for k, v in item.items() if not str(k).startswith("_")}
    if replace_agent and agent_name:
        out["agent_name"] = agent_name.strip()
    elif agent_name and (
        not out.get("agent_name") or out.get("agent_name") in ("YOUR_AGENT_NAME", "onto-agent")
    ):
        out["agent_name"] = agent_name.strip()
    if not out.get("agent_name"):
        raise ValueError(f"模板「{out.get('name', '?')}」缺少 agent_name，请传 --agent-name")
    required_keys = {"name", "match_rules"}
    missing = required_keys - set(out.keys())
    if missing:
        raise ValueError(f"模板「{out.get('name')}」缺少字段: {missing}")
    out.setdefault("match_mode", "rule")
    out.setdefault("required", True)
    out.setdefault("score_weight", 1.0)
    out.setdefault("enabled", True)
    return out


def _http_json(method: str, url: str, body: Dict[str, Any] | None = None) -> Tuple[int, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode("utf-8")
            return resp.status, json.loads(text) if text else None
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(text) if text else {"detail": e.reason}
        except json.JSONDecodeError:
            payload = {"detail": text or e.reason}
        return e.code, payload


def _list_existing_names(api_base: str, agent_name: str) -> Set[str]:
    from urllib.parse import quote

    url = f"{api_base.rstrip('/')}/api/checkpoints/templates?agent_name={quote(agent_name)}"
    status, payload = _http_json("GET", url)
    if status != 200:
        return set()
    if not isinstance(payload, list):
        return set()
    return {str(x.get("name") or "") for x in payload}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量导入过程点模板 JSON")
    parser.add_argument(
        "--json",
        type=Path,
        required=True,
        help="模板 JSON 路径（如 docs/M2-过程点配置-onto-agent识别.json）",
    )
    parser.add_argument(
        "--api-base",
        default="http://127.0.0.1:8000",
        help="onto-eval backend 地址",
    )
    parser.add_argument(
        "--agent-name",
        default=None,
        help="覆盖 JSON 中的 agent_name / YOUR_AGENT_NAME",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将导入的模板，不调用 API",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        default=True,
        help="同名模板已存在则跳过（默认开启）",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_false",
        dest="skip_existing",
        help="不跳过同名（重复时会 409 失败）",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.json.is_file():
        print(f"文件不存在: {args.json}", file=sys.stderr)
        return 1

    try:
        items = _load_templates(args.json)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"读取 JSON 失败: {e}", file=sys.stderr)
        return 1

    api_base = args.api_base.rstrip("/")
    create_url = f"{api_base}/api/checkpoints/templates"

    created = skipped = failed = 0
    existing_cache: Dict[str, Set[str]] = {}

    print(f"共 {len(items)} 条模板，API: {create_url}")
    if args.dry_run:
        print("（dry-run，不写入）\n")

    for i, raw in enumerate(items, 1):
        try:
            body = _normalize_template(
                raw,
                agent_name=args.agent_name,
                replace_agent=bool(args.agent_name),
            )
        except ValueError as e:
            print(f"[{i}] 跳过: {e}")
            failed += 1
            continue

        agent = str(body["agent_name"])
        name = str(body["name"])
        if args.skip_existing:
            if agent not in existing_cache:
                existing_cache[agent] = _list_existing_names(api_base, agent)
            if name in existing_cache[agent]:
                print(f"[{i}] 跳过（已存在）: {agent} / {name}")
                skipped += 1
                continue

        if args.dry_run:
            print(f"[{i}] 将导入: {agent} / {name} order={body.get('order')} required={body.get('required')}")
            created += 1
            continue

        status, payload = _http_json("POST", create_url, body)
        if status == 200:
            tid = payload.get("id") if isinstance(payload, dict) else "?"
            print(f"[{i}] 已创建: {agent} / {name} → {tid}")
            created += 1
            existing_cache.setdefault(agent, set()).add(name)
        elif status == 409:
            print(f"[{i}] 冲突（已存在）: {agent} / {name}")
            skipped += 1
            existing_cache.setdefault(agent, set()).add(name)
        else:
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            print(f"[{i}] 失败 HTTP {status}: {agent} / {name} — {detail}", file=sys.stderr)
            failed += 1

    print(f"\n完成: 创建/待创建 {created}, 跳过 {skipped}, 失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
