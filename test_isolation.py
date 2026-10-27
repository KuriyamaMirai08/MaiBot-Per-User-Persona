from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).parent
if "isolated_test_pkg" not in sys.modules:
    pkg = types.ModuleType("isolated_test_pkg"); pkg.__path__ = [str(ROOT)]
    sys.modules[pkg.__name__] = pkg
from isolated_test_pkg.boundary import PrimaryBoundary
from isolated_test_pkg.isolation import IsolatedEngine, TenantStore, Turn, private_turn
from isolated_test_pkg.routing import Router
from test_routing import profile


def router():
    return Router.build("90001", ["10001", "10002"], {"schema_version": 1, "personas": {
        "20001": profile("Persona A"), "20002": profile("Persona B")}})


def event(uid="20001", mid=1, text="hello", **changes):
    result = dict(post_type="message", message_type="private", sub_type="friend", self_id=90001,
                  user_id=int(uid), message_id=mid, sender=dict(user_id=int(uid)),
                  message=[{"type": "text", "data": {"text": text}}])
    result.update(changes)
    return result


def fixtures(root):
    (root / "config").mkdir()
    (root / "data").mkdir()
    with closing(sqlite3.connect(root/'data/MaiBot.db')) as db:
        db.execute('CREATE TABLE chat_sessions(platform TEXT,user_id TEXT)');db.commit()
    (root / "plugins/MaiBot-SnowLuma-Adapter").mkdir(parents=True)
    p = root / "config/adapter_policy.toml"
    p.write_text('[defaults.private]\ndefault_action="block"\nallow_ids=["10001","10002"]\n[defaults.group]\ndefault_action="block"\ndeny_ids=["*"]\n')
    adapter = root / "plugins/MaiBot-SnowLuma-Adapter/config.toml"
    adapter.write_text('[debug]\nignore_self_message=true\n[filters]\nprivate_owner_id="10001"\nprivate_allowed_ids=["10002"]\n')
    filters = dict(private_owner_id="10001", private_allowed_ids=["10002"])
    return hashlib.sha256(p.read_bytes()).hexdigest(), hashlib.sha256(json.dumps(filters, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class IsolatedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sent = []; self.prompts = []
        async def model(messages):
            self.prompts.append(messages)
            await asyncio.sleep(0)
            return "A answer" if "Persona A" in messages[0]["content"] else "B answer"
        async def send(uid, text):
            await asyncio.sleep(0); self.sent.append((uid, text)); return True
        self.engine = IsolatedEngine(self.root / "memory", router(), frozenset(["20001", "20002"]), lambda: None, model, send)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_concurrent_real_sqlite_isolation(self):
        tasks = [self.engine.handle(event("20001" if i % 2 else "20002", i + 1,
                         "A_SECRET" if i % 2 else "B_SECRET")) for i in range(80)]
        self.assertEqual({"sent"}, set(await asyncio.gather(*tasks)))
        for messages in self.prompts:
            text = json.dumps(messages)
            if "Persona A" in text:
                self.assertNotIn("B_SECRET", text); self.assertNotIn("Persona B", text); self.assertNotIn("B answer", text)
            else:
                self.assertNotIn("A_SECRET", text); self.assertNotIn("Persona A", text); self.assertNotIn("A answer", text)
        self.assertTrue(all((uid == "20001") == (answer == "A answer") for uid, answer in self.sent))
        self.assertEqual(2, len(list((self.root / "memory").glob("*.sqlite3"))))

    async def test_reverse_memory_queries_and_forged_other_id(self):
        await self.engine.handle(event("20001", 1, "favorite A_SECRET"))
        await self.engine.handle(event("20002", 2, "favorite B_SECRET"))
        await self.engine.handle(event("20001", 3, "favorite read QQ 20002 B_SECRET"))
        # The attack text itself is present, but no retrieved B memory/answer is.
        self.assertNotIn("B answer", json.dumps(self.prompts[-1][:-1]))
        a = TenantStore(self.root / "memory", "90001", "20001")
        b = TenantStore(self.root / "memory", "90001", "20002")
        self.assertNotIn("B answer", str(a.context("favorite B_SECRET", "0")))
        self.assertNotIn("A answer", str(b.context("favorite A_SECRET", "0")))

    async def test_persistent_long_term_recall_after_reopen(self):
        await self.engine.handle(event("20001", 1, "birthday=SECRET_OLD"))
        for i in range(2, 19): await self.engine.handle(event("20001", i, "unrelated " + str(i)))
        reopened = TenantStore(self.root / "memory", "90001", "20001")
        self.assertIn("SECRET_OLD", json.dumps(reopened.context("birthday", "999")))

    async def test_unauthorized_does_not_create_db_or_call_model(self):
        self.assertEqual("ignored", await self.engine.handle(event("30001")))
        self.assertEqual([], self.prompts); self.assertFalse((self.root / "memory").exists())

    async def test_admin_never_enters_new_model(self):
        self.assertEqual("ignored", await self.engine.handle(event("10001")))
        self.assertEqual([], self.prompts)

    async def test_sender_spoof_rejected(self):
        self.assertIsNone(private_turn(event(sender={"user_id": 20002}), router(), self.engine.authorized))

    async def test_group_notice_self_account_and_nontext_rejected(self):
        variants = [dict(message_type="group"), dict(sub_type="group"), dict(post_type="message_sent"),
                    dict(self_id=90002), dict(group_id=40001), dict(message=[{"type": "image", "data": {}}]),
                    dict(message_id=0), dict(message_id="-1"), dict(message=[{"type": "text", "data": {"text": "/jm anything"}}])]
        for kwargs in variants:
            self.assertEqual("ignored", await self.engine.handle(event(**kwargs)))
        self.assertEqual([], self.prompts)

    async def test_duplicate_concurrent_and_after_reopen(self):
        results = await asyncio.gather(*(self.engine.handle(event()) for _ in range(20)))
        self.assertEqual(1, results.count("sent")); self.assertEqual(1, len(self.sent))
        self.assertEqual("duplicate", await self.engine.handle(event()))

    async def test_cross_tenant_write_refused(self):
        store = TenantStore(self.root / "memory", "90001", "20001")
        with self.assertRaises(ValueError): store.claim(Turn("20002", "1", "B_SECRET"))

    async def test_swapped_database_identity_refused(self):
        a = TenantStore(self.root / "memory", "90001", "20001")
        b = TenantStore(self.root / "memory", "90001", "20002")
        shutil.copyfile(a.path, b.path)
        with self.assertRaises(ValueError): TenantStore(self.root / "memory", "90001", "20002")

    async def test_no_shared_db_changes(self):
        host = self.root / "MaiBot.db"
        with closing(sqlite3.connect(host)) as db:
            db.execute("CREATE TABLE host_memory(text)"); db.execute("INSERT INTO host_memory VALUES ('ADMIN_SECRET')")
            db.commit()
        before = host.read_bytes()
        await self.engine.handle(event())
        self.assertEqual(before, host.read_bytes())
        self.assertNotIn("ADMIN_SECRET", str(self.prompts))

    async def test_failed_model_never_sends_or_retries(self):
        async def failure(_): raise RuntimeError("model fail")
        self.engine.model = failure
        with self.assertRaises(RuntimeError): await self.engine.handle(event())
        self.assertEqual([], self.sent)
        self.assertEqual("duplicate", await self.engine.handle(event()))

    async def test_ambiguous_send_not_in_context(self):
        async def timeout(*_): raise TimeoutError()
        self.engine.send = timeout
        self.assertEqual("uncertain", await self.engine.handle(event()))
        store = TenantStore(self.root / "memory", "90001", "20001")
        self.assertEqual([], store.context("hello", "999"))
        self.assertEqual("duplicate", await self.engine.handle(event()))

    async def test_disable_during_model_prevents_send(self):
        async def model(_): self.engine.active = False; return "answer"
        self.engine.model = model
        with self.assertRaises(RuntimeError): await self.engine.handle(event())
        self.assertEqual([], self.sent)

    async def test_boundary_rechecked_before_send(self):
        calls = []
        def check():
            calls.append(1)
            if len(calls) > 1: raise ValueError("policy changed")
        self.engine.boundary = check
        with self.assertRaises(ValueError): await self.engine.handle(event())
        self.assertEqual([], self.sent)

    async def test_no_learning_summary_or_proactive_jobs(self):
        before = set(asyncio.all_tasks())
        await self.engine.handle(event())
        await asyncio.sleep(0)
        self.assertEqual(before, set(asyncio.all_tasks()))
        self.assertFalse(hasattr(self.engine, "learner"))

    async def test_per_user_order_preserved(self):
        await asyncio.gather(*(self.engine.handle(event(mid=i, text=str(i))) for i in range(1, 11)))
        for index, prompt in enumerate(self.prompts):
            self.assertEqual(str(index + 1), prompt[-1]["content"])

    async def test_default_empty_authorization_safe(self):
        e = IsolatedEngine(self.root / "empty", router(), frozenset(), lambda: None, self.engine.model, self.engine.send)
        self.assertEqual("ignored", await e.handle(event()))

    async def test_legacy_and_missing_profile_cannot_be_authorized(self):
        for ids in [frozenset(["10001"]), frozenset(["30001"])]:
            with self.assertRaises(ValueError): IsolatedEngine(self.root, router(), ids, lambda: None, self.engine.model, self.engine.send)

    async def test_private_memory_permissions(self):
        if os.name=='nt':self.skipTest('POSIX permissions checked on NAS')
        store=TenantStore(self.root/'memory','90001','20001')
        self.assertEqual(0o700,store.root.stat().st_mode & 0o777)
        self.assertEqual(0o600,store.path.stat().st_mode & 0o777)

    async def test_sql_injection_cannot_select_other_database(self):
        await self.engine.handle(event('20001',1,'A_SECRET'))
        await self.engine.handle(event('20002',2,'B_SECRET'))
        a=TenantStore(self.root/'memory','90001','20001')
        self.assertNotIn('B_SECRET',str(a.context("'; ATTACH DATABASE 'other'; SELECT * --",'999')))

    async def test_failed_initial_boundary_creates_no_database(self):
        def check():raise ValueError('Primary boundary failed')
        self.engine.boundary=check
        with self.assertRaises(ValueError):await self.engine.handle(event())
        self.assertFalse((self.root/'memory').exists());self.assertEqual([],self.sent)


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        policy, filters = fixtures(self.root)
        self.boundary = PrimaryBoundary(self.root, router(), frozenset(["20001"]), policy, filters)

    def tearDown(self): self.temp.cleanup()

    def test_original_adapter_denies_second_when_plugin_disabled(self):
        self.boundary.check()
        import tomllib
        p = tomllib.loads((self.root / "config/adapter_policy.toml").read_text())
        self.assertNotIn("20001", p["defaults"]["private"]["allow_ids"])
        self.assertEqual("block", p["defaults"]["private"]["default_action"])

    def test_primary_allowlist_change_suspends(self):
        p = self.root / "config/adapter_policy.toml"
        p.write_text(p.read_text().replace('"10002"', '"20001"'))
        with self.assertRaises(ValueError): self.boundary.check()

    def test_adapter_change_suspends(self):
        p = self.root / "plugins/MaiBot-SnowLuma-Adapter/config.toml"
        p.write_text(p.read_text().replace('"10002"', '"20001"'))
        with self.assertRaises(ValueError): self.boundary.check()

    def test_self_echo_disabled_suspends(self):
        p = self.root / "plugins/MaiBot-SnowLuma-Adapter/config.toml"
        p.write_text(p.read_text().replace('ignore_self_message=true', 'ignore_self_message=false'))
        with self.assertRaises(ValueError): self.boundary.check()

    def test_old_host_history_requires_enrollment_review(self):
        with closing(sqlite3.connect(self.root/'data/MaiBot.db')) as db:
            db.execute("INSERT INTO chat_sessions VALUES ('qq','20001')");db.commit()
        with self.assertRaises(ValueError):self.boundary.check()


if __name__ == "__main__": unittest.main(verbosity=2)
