from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import hashlib
import json
import shutil
import sqlite3
import tempfile
import tomllib
import unittest

import configure
from test_isolation import fixtures
from test_routing import profile
from isolated_test_pkg.plugin import Settings


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        fixtures(self.root)
        self.plugin=self.root/'plugins/per-user-persona';self.plugin.mkdir()
        (self.root/'config/bot_config.toml').write_text('[bot]\nqq_account="90001"\n',encoding='utf-8')
        with (self.root/'plugins/MaiBot-SnowLuma-Adapter/config.toml').open('a',encoding='utf-8') as f:
            f.write('\n[client]\nclient_type="napcat"\nserver="host.docker.internal"\nport=18080\ntoken="SYNTHETIC_ONLY"\n')
        (self.root/'config/model_config.toml').write_text('''
[model_task_config.replyer]
model_list=["fixture"]
[[models]]
name="fixture"
api_provider="official"
model_identifier="deepseek-flash"
[[api_providers]]
name="official"
base_url="https://api.deepseek.com"
api_key="SYNTHETIC_ONLY"
''',encoding='utf-8')
        self.profiles={'schema_version':1,'personas':{'20001':profile('Example A')}}

    def tearDown(self):self.tmp.cleanup()

    def build(self,**kwargs):return configure.build_plan(self.root,self.plugin,self.profiles,**kwargs)

    def core_digest(self):
        return {str(p.relative_to(self.root)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in [*self.root.glob('config/*'),self.root/'plugins/MaiBot-SnowLuma-Adapter/config.toml',self.root/'data/MaiBot.db']}

    def test_default_configuration_stays_locked_and_disabled(self):
        before=self.core_digest();plan=self.build();cfg=tomllib.loads(plan['config.toml'])
        self.assertFalse(Settings().plugin.enabled)
        self.assertFalse(cfg['plugin']['enabled']);self.assertTrue(cfg['router']['secondary_privacy_lock'])
        self.assertEqual([],cfg['router']['authorized_user_ids']);self.assertEqual(before,self.core_digest())
        self.assertFalse((self.plugin/'config.toml').exists());self.assertNotIn('SYNTHETIC_ONLY',str(plan))

    def test_authorization_and_unlock_are_explicit(self):
        with self.assertRaises(ValueError):self.build(unlock=True)
        for ids in [['10001'],['30001']]:
            with self.assertRaises(ValueError):self.build(authorized=ids,unlock=True)
        plan=self.build(authorized=['20001'],unlock=True)
        cfg=tomllib.loads(plan['config.toml'])
        self.assertEqual(['20001'],cfg['router']['authorized_user_ids'])
        self.assertFalse(cfg['router']['secondary_privacy_lock']);self.assertFalse(cfg['plugin']['enabled'])

    def test_existing_host_history_refuses_enrollment(self):
        db=sqlite3.connect(self.root/'data/MaiBot.db')
        try:
            db.execute("INSERT INTO chat_sessions VALUES ('qq','20001')");db.commit()
        finally:db.close()
        with self.assertRaises(ValueError):self.build(authorized=['20001'],unlock=True)
        self.assertFalse((self.plugin/'config.toml').exists())

    def test_disabled_profile_cannot_be_authorized(self):
        self.profiles['personas']['20001']['enabled']=False
        with self.assertRaises(ValueError):self.build(authorized=['20001'])

    def test_groups_or_self_echo_cannot_be_enabled(self):
        p=self.root/'config/adapter_policy.toml';p.write_text(p.read_text().replace('[defaults.group]\ndefault_action="block"','[defaults.group]\ndefault_action="allow"'))
        with self.assertRaises(ValueError):self.build(authorized=['20001'])

    def test_emoji_requires_authorization_and_audited_helpers(self):
        with self.assertRaises(ValueError):self.build(emoji_for=['20001'])
        fixture=Path(__file__).parent/'tests/fixtures/togawasakiko-desuwa_emoji-select-with-text'
        target=self.root/'plugins'/fixture.name;shutil.copytree(fixture,target)
        plan=self.build(authorized=['20001'],emoji_for=['20001'])
        self.assertEqual({'20001'},set(json.loads(plan['emoji_policy.json'])['users']))
        (target/'select_emoji.prompt').write_text('Changed helper',encoding='utf-8')
        with self.assertRaises(ValueError):self.build(authorized=['20001'],emoji_for=['20001'])

    def test_write_and_replace_back_up_private_config_without_core_changes(self):
        before=self.core_digest();plan=self.build()
        self.assertIsNone(configure.write_plan(self.root,self.plugin,plan))
        with self.assertRaises(ValueError):configure.write_plan(self.root,self.plugin,plan)
        saved=(self.plugin/'config.toml').read_bytes()
        backup=configure.write_plan(self.root,self.plugin,self.build(authorized=['20001'],unlock=True),replace=True)
        self.assertEqual(saved,(backup/'config.toml').read_bytes())
        self.assertEqual(before,self.core_digest())
        self.assertFalse(tomllib.loads((self.plugin/'config.toml').read_text())['plugin']['enabled'])

    def test_plugin_must_be_inside_selected_project(self):
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaises(ValueError):configure.build_plan(self.root,Path(other),self.profiles)

    def test_source_never_imports_shared_conversation_capabilities(self):
        root=Path(__file__).parent
        for name in ['isolation.py','transport.py','plugin.py','emoji_bridge.py']:
            text=(root/name).read_text(encoding='utf-8')
            for capability in ['ctx.send','ctx.llm','ctx.message','ctx.db','ctx.maisaka','ctx.person']:
                self.assertNotIn(capability,text)
        self.assertEqual(['emoji.get_all'],json.loads((root/'_manifest.json').read_text())['capabilities'])
