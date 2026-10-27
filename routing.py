"""Immutable QQ persona routing. Host-independent; no I/O or global mutation."""
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

import re


def qq_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{4,19}", value):
        raise ValueError("QQ ID must be an exact decimal string")
    return value


@dataclass(frozen=True)
class Persona:
    name: str
    background: str
    personality: str
    reply_style: str
    enabled: bool = False

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "Persona":
        fields = {"name", "background", "personality", "reply_style", "enabled"}
        if not isinstance(data, dict) or set(data) - fields:
            raise ValueError("Unknown persona fields")
        if type(data.get("enabled", False)) is not bool:
            raise ValueError("enabled must be a boolean")
        for key in fields - {"enabled"}:
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise ValueError("Persona text fields must be nonempty strings")
            if len(data[key]) > 12000:
                raise ValueError("Persona field too long")
        return cls(**data)

    def prompt(self) -> str:
        return (f"角色名：{self.name}\n背景：{self.background}\n"
                f"人格：{self.personality}\n表达风格：{self.reply_style}")


@dataclass(frozen=True)
class Router:
    account_id: str
    legacy_ids: frozenset[str]
    personas: Mapping[str, Persona]

    @classmethod
    def build(cls, account_id: str, legacy_ids: list[str], data: dict) -> "Router":
        qq_id(account_id)
        if not legacy_ids:
            raise ValueError("Existing allowlist is required; refusing permissive defaults")
        allowed = frozenset(qq_id(x) for x in legacy_ids)
        if not isinstance(data, dict) or set(data) != {"schema_version", "personas"}:
            raise ValueError("Invalid persona document")
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("Unsupported persona schema")
        if not isinstance(data["personas"], dict):
            raise ValueError("personas must be an object")
        profiles = {qq_id(k): Persona.parse(v) for k, v in data["personas"].items()}
        if allowed.intersection(profiles):
            raise ValueError("Existing users are protected from persona overrides")
        return cls(account_id, allowed, MappingProxyType(profiles))

    def decision(self, message: dict, *, outgoing: bool = False) -> str:
        # Trust structured transport identity only, never text/nickname/mentions.
        if not isinstance(message, dict):
            return "block"
        if not isinstance(message.get("platform"), str) or not message["platform"]:
            return "block"
        if message.get("platform") != "qq":
            return "pass"
        info = message.get("message_info")
        if not isinstance(info, dict) or info.get("group_info") is not None:
            return "block"
        if message.get("account_id") != self.account_id:
            return "block"
        user = info.get("user_info")
        if outgoing:
            metadata = info.get("additional_config")
            uid = metadata.get("platform_io_target_user_id") if isinstance(metadata, dict) else None
        else:
            uid = user.get("user_id") if isinstance(user, dict) else None
        if not isinstance(uid, str):
            return "block"
        if uid in self.legacy_ids:
            return "pass"
        # Deliberately cannot unlock through configuration. An audited new version
        # is required before any secondary message enters shared host pipelines.
        return "block"

    def preview_items(self, user_id: str, items: list[dict]) -> list[dict]:
        """Offline prompt projection only. Does not authorize or send messages.

        This proves request-local persona selection, NOT privacy isolation. Host
        histories/tool results/background learners are outside this function.
        """
        profile = self.personas.get(qq_id(user_id))
        result = deepcopy(items)
        if profile is None or not profile.enabled:
            return result
        system = [x for x in result if x.get("item_type") == "SystemMessageItem"]
        if len(system) != 1:
            raise ValueError("Preview requires exactly one explicit system item")
        system[0]["parts"] = [{"type": "text", "text": profile.prompt()}]
        return result
