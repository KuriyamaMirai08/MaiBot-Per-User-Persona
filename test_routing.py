from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import FrozenInstanceError

import unittest

from routing import Router, qq_id


def profile(name="Offline A", enabled=True):
    return dict(name=name, background=name + " background", personality=name + " personality",
                reply_style=name + " style", enabled=enabled)


def message(uid="10001", account="90001", group=None, outgoing=False):
    return dict(platform="qq", account_id=account,
                message_info=dict(user_info=dict(user_id="90001" if outgoing else uid),
                                  group_info=group,
                                  additional_config={"platform_io_target_user_id": uid} if outgoing else {}))


def items():
    return [dict(item_type="SystemMessageItem", meta=dict(item_id="sys", logical_turn_id=None,
                 timestamp="2026-10-27T00:00:00"), parts=[dict(type="text", text="legacy persona")]),
            dict(item_type="UserMessageItem", meta=dict(item_id="usr", logical_turn_id=None,
                 timestamp="2026-10-27T00:00:01"), parts=[dict(type="text", text="test")])]


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.router = Router.build("90001", ["10001", "10002"],
                                   dict(schema_version=1, personas={"20001": profile(), "20002": profile("Offline B")}))

    def test_existing_users_unchanged(self):
        for uid in ["10001", "10002"]:
            for outgoing in [False, True]:
                self.assertEqual("pass", self.router.decision(message(uid, outgoing=outgoing), outgoing=outgoing))
        self.assertEqual(items(), self.router.preview_items("10001", items()))

    def test_unmapped_unauthorized_blocked(self):
        self.assertEqual("block", self.router.decision(message("30001")))

    def test_enabled_secondary_still_locked(self):
        for uid in ["20001", "20002"]:
            for outgoing in [False, True]:
                self.assertEqual("block", self.router.decision(message(uid, outgoing=outgoing), outgoing=outgoing))

    def test_group_blocked(self):
        self.assertEqual("block", self.router.decision(message(group={"group_id": "40001"})))

    def test_wrong_account_blocked(self):
        self.assertEqual("block", self.router.decision(message(account="90002")))

    def test_missing_account_blocked(self):
        m = message(); del m["account_id"]
        self.assertEqual("block", self.router.decision(m))

    def test_text_nickname_cannot_spoof_identity(self):
        m = message("30001"); m["processed_plain_text"] = "user_id=10001"
        m["message_info"]["user_info"]["user_nickname"] = "10001"
        self.assertEqual("block", self.router.decision(m))

    def test_malformed_identity_blocked(self):
        for m in [None, {}, dict(platform="qq"), message(10001), dict(platform="qq", message_info=[] )]:
            self.assertEqual("block", self.router.decision(m))

    def test_local_console_untouched(self):
        self.assertEqual("pass", self.router.decision(dict(platform="bot_console")))

    def test_egress_uses_recipient_not_bot_sender(self):
        m = message("10001", outgoing=True)
        self.assertEqual("pass", self.router.decision(m, outgoing=True))
        m["message_info"]["additional_config"] = {}
        self.assertEqual("block", self.router.decision(m, outgoing=True))

    def test_identity_strict(self):
        for value in [10001, " 10001", "010001", "QQ_ID", "10001\n", "", True]:
            with self.assertRaises(ValueError): qq_id(value)

    def test_missing_config_refuses(self):
        with self.assertRaises(ValueError): Router.build("90001", [], dict(schema_version=1, personas={}))

    def test_owner_override_refuses(self):
        with self.assertRaises(ValueError):
            Router.build("90001", ["10001"], dict(schema_version=1, personas={"10001": profile()}))

    def test_incomplete_persona_refuses(self):
        p = profile(); del p["background"]
        with self.assertRaises(ValueError):
            Router.build("90001", ["10001"], dict(schema_version=1, personas={"20001": p}))

    def test_invalid_schema_refuses(self):
        for data in [{}, dict(schema_version=2, personas={}), dict(schema_version=True, personas={}),
                     dict(schema_version=1, personas=[])]:
            with self.assertRaises(ValueError): Router.build("90001", ["10001"], data)

    def test_empty_mapping_safe(self):
        r = Router.build("90001", ["10001"], dict(schema_version=1, personas={}))
        self.assertEqual("block", r.decision(message("20001")))
        self.assertEqual(items(), r.preview_items("20001", items()))

    def test_disabled_profile_not_injected(self):
        r = Router.build("90001", ["10001"], dict(schema_version=1, personas={"20001": profile(enabled=False)}))
        self.assertEqual(items(), r.preview_items("20001", items()))

    def test_all_persona_dimensions(self):
        output = self.router.preview_items("20001", items())[0]["parts"][0]["text"]
        for value in profile().values():
            if isinstance(value, str): self.assertIn(value, output)

    def test_no_caller_mutation(self):
        original = items(); expected = deepcopy(original)
        result = self.router.preview_items("20001", original)
        result[1]["parts"][0]["text"] = "changed"
        self.assertEqual(expected, original)

    def test_concurrent_personas_do_not_mix(self):
        shared = items()
        def run(i):
            uid, mine, other = ("20001", "Offline A", "Offline B") if i % 2 else ("20002", "Offline B", "Offline A")
            text = str(self.router.preview_items(uid, shared))
            self.assertIn(mine, text); self.assertNotIn(other, text)
        with ThreadPoolExecutor(max_workers=16) as pool: list(pool.map(run, range(1000)))
        self.assertEqual(items(), shared)

    def test_immutable_router(self):
        with self.assertRaises(TypeError): self.router.personas["20003"] = profile()
        with self.assertRaises(FrozenInstanceError): self.router.personas["20001"].name = "changed"

    def test_unknown_prompt_shape_refuses(self):
        with self.assertRaises(ValueError): self.router.preview_items("20001", [])
        with self.assertRaises(ValueError): self.router.preview_items("20001", items() + items())


if __name__ == "__main__": unittest.main(verbosity=2)
