"""Fail closed unless the original host still refuses all secondary users."""
from pathlib import Path

import hashlib
import json
import sqlite3
import tomllib

from .routing import Router


class PrimaryBoundary:
    def __init__(self, root: Path, router: Router, authorized: frozenset[str], policy_hash: str, filters_hash: str):
        self.root, self.router, self.authorized = root, router, authorized
        self.policy_hash, self.filters_hash = policy_hash, filters_hash

    def check(self):
        raw = (self.root / "config/adapter_policy.toml").read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.policy_hash:
            raise ValueError("Original adapter policy changed; isolation suspended")
        policy = tomllib.loads(raw.decode())
        if set(policy) != {"defaults"}:
            raise ValueError("Unreviewed primary policy override")
        private = policy["defaults"]["private"]
        if set(private.get("allow_ids", [])) != set(self.router.legacy_ids) or private.get("default_action") != "block":
            raise ValueError("Original host must deny secondary users")
        adapter = tomllib.loads((self.root / "plugins/MaiBot-SnowLuma-Adapter/config.toml").read_text())
        filters = adapter["filters"]
        digest = hashlib.sha256(json.dumps(filters, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if digest != self.filters_hash or adapter["debug"].get("ignore_self_message") is not True:
            raise ValueError("Original receive filters changed; isolation suspended")
        original = {filters["private_owner_id"], *filters.get("private_allowed_ids", [])}
        if self.authorized.intersection(original | self.router.legacy_ids):
            raise ValueError("A secondary user is also accepted by original adapter")
        if self.authorized:
            # Metadata only: do not import any old Host conversation contents.
            db = sqlite3.connect(f"file:{self.root / 'data/MaiBot.db'}?mode=ro", uri=True)
            try:
                placeholders = ','.join('?' for _ in self.authorized)
                count = db.execute("SELECT COUNT(*) FROM chat_sessions WHERE platform='qq' AND user_id IN (" + placeholders + ")",
                                   sorted(self.authorized)).fetchone()[0]
                if count:
                    raise ValueError("Secondary user already has Host history; enrollment requires review")
            finally:
                db.close()
