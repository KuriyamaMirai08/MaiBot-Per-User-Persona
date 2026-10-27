"""Read-only, allowlisted administrator projections of isolated tenant DBs."""
from pathlib import Path

import hashlib
import sqlite3

from .routing import qq_id


def read_history(root: Path, account_id: str, user_id: str, authorized, *, before=0, errors_only=False):
    if qq_id(user_id) not in authorized:
        raise PermissionError("Isolated chat not authorized")
    if type(before) is not int or before < 0 or before > 2**63-1 or type(errors_only) is not bool:
        raise ValueError("Invalid history cursor")
    tenant = hashlib.sha256(f"qq:{qq_id(account_id)}:{user_id}".encode()).hexdigest()
    if root.is_symlink():
        raise ValueError("Unsafe history directory")
    path = root / (tenant + ".sqlite3")
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise ValueError("Unsafe history file")
    if not path.exists():
        return dict(rows=[], next_before=0, has_more=False)
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    try:
        if [row[0] for row in db.execute("SELECT tenant FROM identity")] != [tenant]:
            raise ValueError("History tenant identity mismatch")
        has_audit = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_audit'").fetchone())
        has_emoji = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='emoji_events'").fetchone())
        query = "SELECT t.rowid AS cursor,t.event_id,t.created,t.user_text,t.assistant_text,t.status"
        query += ",a.updated,a.model_state,a.model,a.elapsed_ms,a.error_stage,a.error_code,a.finish_reason" if has_audit else ""
        query += ",e.state AS emoji_state,e.description AS emoji_description,e.updated AS emoji_at,e.error_code AS emoji_error" if has_emoji else ""
        query += " FROM turns t LEFT JOIN turn_audit a ON a.event_id=t.event_id" if has_audit else " FROM turns t"
        if has_emoji:
            query += ' LEFT JOIN emoji_events e ON e.event_id=t.event_id'
        query += " WHERE (?=0 OR t.rowid<?)"
        if errors_only:
            query += " AND (t.status IN ('failed','uncertain')" + (" OR e.state IN ('failed','uncertain')" if has_emoji else "") + ")"
        query += " ORDER BY t.rowid DESC LIMIT 6"
        rows = [dict(row) for row in db.execute(query, (before, before))]
        more = len(rows) > 5
        rows = rows[:5]
        return dict(rows=list(reversed(rows)), next_before=min((x['cursor'] for x in rows), default=0), has_more=more)
    finally:
        db.close()
