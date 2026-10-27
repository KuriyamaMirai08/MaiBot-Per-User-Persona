"""Per-persona response policy; no global model configuration changes."""
from dataclasses import dataclass
from types import MappingProxyType

import json
import re


@dataclass(frozen=True)
class ReplyPolicy:
    enabled: bool = False
    temperature: float = 0.65
    max_tokens: int = 256
    assistant_context_after: float = 0
    allow_ack_silence: bool = False

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown response policy fields")
        result = cls(**data)
        if type(result.enabled) is not bool or type(result.allow_ack_silence) is not bool:
            raise ValueError("Response policy flags must be booleans")
        if type(result.temperature) not in (int, float) or not 0 <= result.temperature <= 1:
            raise ValueError("Response policy temperature out of range")
        if type(result.max_tokens) is not int or not 64 <= result.max_tokens <= 1024:
            raise ValueError("Response budget out of range")
        if type(result.assistant_context_after) not in (int, float) or not 0 <= result.assistant_context_after < 1e11:
            raise ValueError("Response history epoch out of range")
        return result

    def budget(self, text):
        if len(text) <= 5:
            return min(self.max_tokens, 96)
        return self.max_tokens if len(text) < 80 else min(512, self.max_tokens * 2)

    def skip_ack(self, text, previous_assistant):
        # Only close a real existing exchange, never ignore a first greeting,
        # emotional disclosure, question, or reply to an unanswered question.
        ack = re.sub(r"[。.!！\s]+$", "", text.strip()).lower()
        return bool(self.enabled and self.allow_ack_silence and previous_assistant and
                    ack in {"行", "嗯", "哦", "好", "好的", "ok", "okok", "知道了", "收到"} and
                    not re.search(r"[?？]|(?:吗|呢)[。！!\s]*$", previous_assistant))


@dataclass(frozen=True)
class ModelReply:
    text: str
    model: str = ""
    finish_reason: str = ""


class ModelFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def load_policies(path, authorized):
    if not path.exists():
        return MappingProxyType({})
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"schema_version", "users"} or data["schema_version"] != 1:
        raise ValueError("Invalid response policy document")
    if not isinstance(data["users"], dict) or set(data["users"]) - set(authorized):
        raise ValueError("Policy may only address approved isolated users")
    return MappingProxyType({uid: ReplyPolicy.parse(value) for uid, value in data["users"].items()})
