"""Real installed Host checks; no real send, model call, or memory mutation."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import ast
import importlib.util
import importlib
import json
import os
import tempfile
import sys
import types
import unittest

from test_isolation import fixtures, router
from test_routing import message
from isolated_test_pkg.plugin import PerUserPersona, create_plugin


class SDKContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_factory_and_legacy_hooks(self):
        p=create_plugin(); p.router=router()
        self.assertIsInstance(p,PerUserPersona)
        self.assertEqual(4,len(p.get_components()))
        for uid in ['10001','10002']:
            self.assertEqual({'action':'continue'},await p.guard_receive(message=message(uid)))
            self.assertEqual({'action':'continue'},await p.guard_send(message=message(uid,outgoing=True)))
        self.assertEqual({'action':'abort'},await p.guard_receive(message=message('20001')))
        self.assertEqual({'action':'abort'},await p.guard_send(message=message('20001',outgoing=True)))

    async def test_disabled_and_empty_config_create_no_socket_or_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);policy,filters=fixtures(root)
            directory=root/'plugins/per-user-persona';directory.mkdir()
            (directory/'personas.json').write_text(json.dumps({'schema_version':1,'personas':{}}),encoding='utf-8')
            cfg={'plugin':{'config_version':'0.2.3','enabled':False},'router':{
                'account_id':'90001','legacy_user_ids':['10001','10002'],'authorized_user_ids':[],
                'secondary_privacy_lock':True,'primary_policy_sha256':policy,'adapter_filters_sha256':filters}}
            p=create_plugin();p.set_plugin_config(cfg);p._ctx=SimpleNamespace(logger=Mock())
            module=importlib.import_module('isolated_test_pkg.plugin')
            with patch.object(module,'__file__',str(directory/'plugin.py')):
                await p.on_load()
            self.assertIsNone(p.task);self.assertIsNone(p.engine);self.assertIsNone(p.client)
            await p.on_unload()

    async def test_real_adapter_drops_secondary_before_codec_or_gateway(self):
        root=Path('/MaiMBot')
        if os.environ.get('PERSONA_INSTALLED_HOST_TESTS')!='1' or not root.exists(): self.skipTest('Opt-in installed Host contract')
        import tomllib
        adapter=root/'plugins/MaiBot-SnowLuma-Adapter'
        pkg=types.ModuleType('persona_adapter_contract');pkg.__path__=[str(adapter)];sys.modules[pkg.__name__]=pkg
        module=importlib.import_module(pkg.__name__+'.runtime.router')
        cfg=tomllib.loads((adapter/'config.toml').read_text())
        settings=SimpleNamespace(filters=SimpleNamespace(ban_user_id=[],private_owner_id=cfg['filters']['private_owner_id'],private_allowed_ids=cfg['filters']['private_allowed_ids']))
        gateway=SimpleNamespace(route_message=AsyncMock())
        codec=SimpleNamespace(build_message_dict=AsyncMock())
        r=module.QQEventRouter(gateway,Mock(),'offline-contract',lambda:settings)
        r.bind_runtime(SimpleNamespace(inbound_codec=codec))
        from test_isolation import event
        for uid in ['20001','20002','30001']:
            await r.handle_inbound_message(event(uid))
        gateway.route_message.assert_not_awaited();codec.build_message_dict.assert_not_awaited()


class InstalledHostTests(unittest.TestCase):
    def setUp(self):
        self.root=Path('/MaiMBot')
        if os.environ.get('PERSONA_INSTALLED_HOST_TESTS')!='1' or not self.root.exists(): self.skipTest('Opt-in installed Host contract')

    def test_real_primary_policy_refuses_fictional_ids_without_plugin(self):
        from src.platform_io.adapter_policy import AdapterIdentity,AdapterPolicyManager
        import tomllib
        policy=AdapterPolicyManager(self.root/'config/adapter_policy.toml')
        account=tomllib.loads((self.root/'config/bot_config.toml').read_text())['bot']['qq_account']
        identity=AdapterIdentity(plugin_id='maibot-team.snowluma-adapter',platform='qq',account_id=account)
        for uid in ['20001','20002','30001']:
            self.assertFalse(policy.evaluate(identity,chat_type='private',target_id=uid).allowed)
        legacy=tomllib.loads((self.root/'config/adapter_policy.toml').read_text())['defaults']['private']['allow_ids']
        for uid in legacy:self.assertTrue(policy.evaluate(identity,chat_type='private',target_id=uid).allowed)

    def test_standard_send_has_unconditional_memory_notification(self):
        tree=ast.parse((self.root/'src/services/send_service.py').read_text())
        found=False
        for node in ast.walk(tree):
            if isinstance(node,ast.If) and any(isinstance(x,ast.Name) and x.id=='storage_message' for x in ast.walk(node.test)):
                self.assertFalse(any(isinstance(x,ast.Name) and x.id=='_notify_memory_automation_on_message_sent' for x in ast.walk(node)))
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='_notify_memory_automation_on_message_sent':found=True
        self.assertTrue(found)

    def test_original_inbound_persists_before_memory_notification(self):
        source=(self.root/'src/chat/heart_flow/heartflow_message_processor.py').read_text()
        self.assertLess(source.index('store_message_to_db_async'),source.index('memory_automation_service.on_incoming_message'))

    def test_new_pipeline_imports_no_host_or_shared_capabilities(self):
        here=Path(__file__).parent
        for name in ['isolation.py','transport.py','boundary.py','plugin.py']:
            source=(here/name).read_text();tree=ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node,ast.ImportFrom):self.assertFalse((node.module or '').startswith('src.'))
            for capability in ['ctx.send','ctx.llm','ctx.message','ctx.db','ctx.maisaka','ctx.person']:
                self.assertNotIn(capability,source)
        manifest=json.loads((here/'_manifest.json').read_text());self.assertEqual(['emoji.get_all'],manifest['capabilities'])

    def test_main_database_never_migrated_by_plugin(self):
        source=(Path(__file__).parent/'isolation.py').read_text()
        self.assertNotIn('MaiBot.db',source);self.assertNotIn('ATTACH DATABASE',source.upper())


if __name__=='__main__':unittest.main(verbosity=2)
