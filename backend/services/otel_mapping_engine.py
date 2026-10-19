"""
OTel 属性映射引擎（spec §7.3 / 方案 §5.5.3-B）
Provider 级 YAML 配置驱动：把 observation name + type 映射为 agent.* 属性。
映射后 derive_aggregate_key 走 agent.step.id 正常路径，告别 legacy: 回退。
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Mapping, Optional

import yaml

from config import BASE_DIR

_MAPPING_FILE = os.path.join(BASE_DIR, "backend", "data", "otel_attribute_mapping.yaml")


class _MappingRule:
    """单条映射规则。"""

    def __init__(self, when: dict, set_actions: dict):
        self.when_type: Optional[str] = when.get("as_type")
        self.name_regex: Optional[re.Pattern] = None
        if "name_regex" in when:
            self.name_regex = re.compile(when["name_regex"])
        self.set_actions: Dict[str, str] = set_actions

    def match(self, obs: Mapping[str, Any]) -> Optional[re.Match]:
        """
        返回 re.Match（regex 匹配）、True（无 regex 但类型匹配）、None（不匹配）。
        """
        if self.when_type:
            obs_type = (obs.get("type") or "").lower()
            if obs_type != self.when_type.lower():
                return None
        if self.name_regex:
            name = obs.get("name") or ""
            m = self.name_regex.match(name)
            return m if m else None
        return True


class ProviderMappingEngine:
    """Provider 级属性映射引擎。每 trace 实例化一次（含 tool 计数器状态）。"""

    _config_cache: Optional[dict] = None

    def __init__(self, provider: str):
        self.provider = provider
        self.rules: List[_MappingRule] = []
        self._tool_counter = 0
        self._load_rules()

    @classmethod
    def _load_config(cls) -> dict:
        if cls._config_cache is None:
            try:
                with open(_MAPPING_FILE, "r", encoding="utf-8") as f:
                    cls._config_cache = yaml.safe_load(f) or {}
            except (FileNotFoundError, yaml.YAMLError):
                cls._config_cache = {}
        return cls._config_cache

    def _load_rules(self):
        config = self._load_config()

        # 兼容两种 YAML 格式：
        #   旧格式（单 provider）：顶层 provider: xxx + rules: [...]
        #   新格式（多 provider）：providers: { xxx: { rules: [...] } }
        if "providers" in config and isinstance(config["providers"], dict):
            provider_config = config["providers"].get(self.provider, {})
        elif config.get("provider") == self.provider:
            provider_config = config
        else:
            provider_config = {}

        if not isinstance(provider_config, dict):
            return

        for rule_def in provider_config.get("rules", []):
            when = rule_def.get("when", {})
            set_actions = rule_def.get("set", {})
            self.rules.append(_MappingRule(when, set_actions))

    def apply(
        self, obs: Mapping[str, Any], attrs: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        对单个 observation 应用映射规则，修改 attrs 并返回。
        仅当 attrs 缺少 agent.step.id 时才应用（不覆盖 D1c 已写属性）。
        """
        # 已有规范属性（D1c 直接写的 team_event span），跳过
        if attrs.get("agent.step.id"):
            return attrs

        for rule in self.rules:
            match = rule.match(obs)
            if match is None:
                continue

            is_tool = (
                rule.when_type is not None and rule.when_type.lower() == "tool"
            )

            for key, template in rule.set_actions.items():
                value = self._resolve_template(template, obs, match)
                if value is not None:
                    attrs[key] = value

            if is_tool:
                self._tool_counter += 1

            break  # 只应用第一条匹配规则

        return attrs

    def _resolve_template(
        self, template: str, obs: Mapping[str, Any], regex_match: Any
    ) -> Any:
        if not isinstance(template, str):
            return template

        # 整体匹配 ${observation.name}
        if template == "${observation.name}":
            return obs.get("name", "")

        # 整体匹配 ${sibling_order_among_tools}
        if template == "${sibling_order_among_tools}":
            return self._tool_counter

        # 整体匹配 ${regex.group(N)}
        m = re.match(r"^\$\{regex\.group\((\d+)\)\}$", template)
        if m and isinstance(regex_match, re.Match):
            return regex_match.group(int(m.group(1)))

        # 混合插值（如 "tool:${observation.name}"）
        result = template
        result = result.replace(
            "${observation.name}", str(obs.get("name", ""))
        )
        result = result.replace(
            "${sibling_order_among_tools}", str(self._tool_counter)
        )
        if isinstance(regex_match, re.Match):
            result = re.sub(
                r"\$\{regex\.group\((\d+)\)\}",
                lambda g: str(regex_match.group(int(g.group(1)))),
                result,
            )
        return result


def create_mapping_engine(
    provider: Optional[str],
) -> Optional[ProviderMappingEngine]:
    """工厂方法：provider 为 None 或无规则时返回 None（无操作）。"""
    if not provider:
        return None
    engine = ProviderMappingEngine(provider)
    return engine if engine.rules else None
