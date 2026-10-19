"""onto-eval 后端 HTTP 客户端，标准库 urllib only。"""
from __future__ import annotations

import json
from typing import Any
from urllib import error, request


class BackendClient:
    """调用 onto-eval 后端的 HTTP 客户端。

    标准库 urllib only。token 通过 X-Eval-Token header 传递，后端
    EVAL_SINGLE_TOKEN 环境变量校验。token 静态，无刷新逻辑。
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: int,
        config: dict[str, Any],
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.config = config

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "BackendClient":
        return cls(
            config["backend_url"],
            config.get("token", ""),
            int(config.get("request_timeout", 200)),
            config,
        )

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["X-Eval-Token"] = self.token

        data = (
            json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
        )
        req = request.Request(url, data=data, method=method, headers=headers)

        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                return json.loads(raw)
        except error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            # 透传后端 JSON 错误（如 400 unknown_metric / 401 invalid_token）
            try:
                err_body = json.loads(body_text)
            except (json.JSONDecodeError, ValueError):
                err_body = {"error": "http_error", "message": f"HTTP {exc.code}: {body_text}"}
            raise RuntimeError(f"HTTP {exc.code}: {err_body}") from exc
        except error.URLError as exc:
            raise RuntimeError(
                f"无法连接 onto-eval 后端 ({self.base_url}): {exc.reason}. "
                f"请检查 config.json 的 backend_url。"
            ) from exc

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body)

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)
