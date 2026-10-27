from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest

from test_isolation import event, router
from isolated_test_pkg.history_view import read_history
from isolated_test_pkg.isolation import IsolatedEngine, TenantStore, Turn
from isolated_test_pkg.reply_policy import ModelFailure, ModelReply, ReplyPolicy


class HistoryPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=TenantStore(self.root,'90001','20001')
        self.sent=[];self.prompts=[]
        async def model(messages):self.prompts.append(messages);return 'answer'
        async def send(uid,text):self.sent.append((uid,text));return True
        self.engine=IsolatedEngine(self.root,router(),frozenset(['20001','20002']),lambda:None,model,send)

    async def asyncTearDown(self):self.tmp.cleanup()

    def seed(self,mid,text,answer):
        self.store.claim(Turn('20001',str(mid),text));self.store.finish(str(mid),answer,'sent')

    async def test_history_is_read_only_and_tenant_scoped(self):
        self.seed(1,'A_SECRET','A answer')
        b=TenantStore(self.root,'90001','20002');b.claim(Turn('20002','2','B_SECRET'));b.finish('2','B answer','sent')
        before=self.store.path.read_bytes()
        result=read_history(self.root,'90001','20001',frozenset(['20001','20002']))
        self.assertIn('A_SECRET',str(result));self.assertNotIn('B_SECRET',str(result));self.assertEqual(before,self.store.path.read_bytes())

    async def test_history_cannot_view_admin_or_unknown(self):
        for uid in ['10001','30001','../../bad']:
            with self.assertRaises((ValueError,PermissionError)):read_history(self.root,'90001',uid,frozenset(['20001']))

    async def test_history_cursor_and_pagination(self):
        for i in range(1,13):self.seed(i,'user '+str(i),'answer')
        first=read_history(self.root,'90001','20001',frozenset(['20001']))
        second=read_history(self.root,'90001','20001',frozenset(['20001']),before=first['next_before'])
        self.assertEqual(5,len(first['rows']));self.assertTrue(first['has_more'])
        self.assertFalse({x['event_id'] for x in first['rows']}.intersection(x['event_id'] for x in second['rows']))
        for cursor in [-1,True,'1',2**64]:
            with self.assertRaises(ValueError):read_history(self.root,'90001','20001',frozenset(['20001']),before=cursor)

    async def test_legacy_rows_show_no_invented_model_metadata(self):
        self.seed(1,'old user','old answer')
        row=read_history(self.root,'90001','20001',frozenset(['20001']))['rows'][0]
        self.assertIsNone(row['model_state']);self.assertIsNone(row['elapsed_ms'])

    async def test_audit_records_model_success_and_errors_safely(self):
        await self.engine.handle(event())
        first=read_history(self.root,'90001','20001',frozenset(['20001']))['rows'][0]
        self.assertEqual('succeeded',first['model_state'])
        async def fail(_):raise RuntimeError('API_KEY_SECRET body USER_SECRET')
        self.engine.model=fail
        with self.assertRaises(RuntimeError):await self.engine.handle(event(mid=2))
        errors=read_history(self.root,'90001','20001',frozenset(['20001']),errors_only=True)
        self.assertEqual('RuntimeError',errors['rows'][0]['error_code'])
        self.assertNotIn('API_KEY_SECRET',str(errors));self.assertNotIn('USER_SECRET',str(errors))

    async def test_old_assistant_pollution_excluded_but_not_deleted(self):
        self.seed(1,'user birthday','刚在听歌差点没看到消息 POLLUTION')
        cutoff=time.time()+1
        self.engine.policies={'20001':ReplyPolicy(enabled=True,assistant_context_after=cutoff)}
        await self.engine.handle(event(mid=2,text='birthday'))
        self.assertNotIn('POLLUTION',str(self.prompts));self.assertIn('user birthday',str(self.prompts))
        self.assertIn('POLLUTION',str(read_history(self.root,'90001','20001',frozenset(['20001']))))

    async def test_ack_can_end_exchange_without_model_or_send(self):
        self.seed(1,'先去忙了','好，先去吧。')
        self.engine.policies={'20001':ReplyPolicy(enabled=True,allow_ack_silence=True)}
        self.assertEqual('no_reply',await self.engine.handle(event(mid=2,text='行')))
        self.assertEqual([],self.prompts);self.assertEqual([],self.sent)
        row=read_history(self.root,'90001','20001',frozenset(['20001']))['rows'][-1]
        self.assertEqual('no_reply',row['status']);self.assertEqual('skipped',row['model_state'])

    async def test_first_ack_and_emotional_messages_are_not_silenced(self):
        self.engine.policies={'20001':ReplyPolicy(enabled=True,allow_ack_silence=True)}
        self.assertEqual('sent',await self.engine.handle(event(mid=1,text='行')))
        self.assertEqual('sent',await self.engine.handle(event(mid=2,text='今天累死我了')))

    async def test_ack_to_question_still_generates(self):
        self.seed(1,'hi','要不要听听这个想法？')
        self.engine.policies={'20001':ReplyPolicy(enabled=True,allow_ack_silence=True)}
        self.assertEqual('sent',await self.engine.handle(event(mid=2,text='行')))

    async def test_policy_only_applies_to_named_tenant(self):
        self.engine.policies={'20001':ReplyPolicy(enabled=True,allow_ack_silence=True)}
        b=TenantStore(self.root,'90001','20002');b.claim(Turn('20002','1','先忙'));b.finish('1','好。','sent')
        self.assertEqual('sent',await self.engine.handle(event('20002',2,'行')))

    async def test_generation_parameters_and_model_metadata_are_per_call(self):
        class Model:
            async def generate(inner,messages,**kwargs):
                self.assertEqual(.65,kwargs['temperature']);self.assertEqual(96,kwargs['max_tokens'])
                return ModelReply('在。','offline-model','stop')
        self.engine.model=Model();self.engine.policies={'20001':ReplyPolicy(enabled=True)}
        await self.engine.handle(event(text='在吗'))
        row=read_history(self.root,'90001','20001',frozenset(['20001']))['rows'][0]
        self.assertEqual('offline-model',row['model']);self.assertEqual('stop',row['finish_reason'])

    async def test_history_can_read_pre_upgrade_schema_without_migration(self):
        with self.store.connect() as db:db.execute('DROP TABLE turn_audit')
        before=self.store.path.read_bytes()
        read_history(self.root,'90001','20001',frozenset(['20001']))
        self.assertEqual(before,self.store.path.read_bytes())

    async def test_policy_ranges_fail_closed(self):
        for data in [dict(temperature=1.5),dict(max_tokens=2),dict(allow_ack_silence='yes'),dict(extra=True)]:
            with self.assertRaises(ValueError):ReplyPolicy.parse(data)

    async def test_page_uses_text_not_html_and_private_static_apis(self):
        p=Path(__file__).parent
        html=(p/'history.html').read_text(encoding='utf-8')
        self.assertIn('textContent',html);self.assertNotIn('innerHTML',html);self.assertNotIn('fetch(',html)
        from isolated_test_pkg.plugin import create_plugin
        api=[x for x in create_plugin().get_components() if x['type']=='API']
        self.assertEqual(2,len(api));self.assertTrue(all(x['metadata']['public'] is False for x in api))


if __name__=='__main__':unittest.main(verbosity=2)
