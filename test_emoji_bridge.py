from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncio
import base64
import json
import shutil
import tempfile
import unittest

from PIL import Image

from test_isolation import event, router
from isolated_test_pkg.emoji_bridge import EmojiBridge, EmojiPolicy, SelectorHelpers, request_kind, validated_image
from isolated_test_pkg.history_view import read_history
from isolated_test_pkg.isolation import IsolatedEngine, TenantStore, Turn
from isolated_test_pkg.reply_policy import ModelReply, ReplyPolicy
from isolated_test_pkg.transport import Credentials, OneBotTextClient


def png():
    stream=BytesIO();Image.new('RGB',(2,2),'red').save(stream,format='PNG')
    return base64.b64encode(stream.getvalue()).decode()


class EmojiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        path=Path('/MaiMBot/plugins')
        if not (path/'togawasakiko-desuwa_emoji-select-with-text').exists():path=Path(__file__).parent/'tests/fixtures'
        hashes=json.loads(Path(__file__).with_name('emoji_policy.example.json').read_text())['source_hashes']
        self.helpers=SelectorHelpers(path,hashes)
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.texts=[];self.images=[];self.selection_prompts=[]
        self.library=AsyncMock(return_value={'success':True,'emojis':[
            {'description':'微笑 开心','base64':png()}, {'description':'无奈 叹气','base64':png()}]})
        self.choice='{"selected":1}'
        outer=self
        class Model:
            async def generate(self,messages,**kwargs):
                outer.selection_prompts.append(messages)
                return ModelReply(outer.choice,'mock-selector','stop')
        async def text_model(messages):return 'A_REPLY' if 'Persona A' in messages[0]['content'] else 'B_REPLY'
        async def text_send(uid,text):self.texts.append((uid,text));return True
        async def image_send(uid,data):self.images.append((uid,data));return True
        policies={uid:EmojiPolicy(enabled=True,auto_probability=.5) for uid in ['20001','20002']}
        self.engine=IsolatedEngine(self.root,router(),frozenset(policies),lambda:None,text_model,text_send)
        self.bridge=EmojiBridge(policies,self.helpers,self.library,Model(),image_send,lambda:None,lambda:self.engine.active,random_draw=lambda:0)
        self.engine.emoji=self.bridge

    async def asyncTearDown(self):self.tmp.cleanup()

    def store(self,uid='20001'):return TenantStore(self.root,'90001',uid)

    async def test_reuses_actual_installed_pure_selector_helpers(self):
        await self.engine.handle(event(text='发个开心的表情包'))
        self.assertEqual(1,len(self.images));self.assertEqual('20001',self.images[0][0])
        self.assertIn('1. 微笑 开心',self.selection_prompts[0][1]['content'])

    async def test_no_match_and_invalid_index_never_random_fallback(self):
        for i,choice in enumerate(['{"selected":0}','{"selected":99}','{"selected":true}','{"selected":1.0}',
                                   '{"selected":1,"url":"bad"}','{"selected":1} {"selected":2}','not json']):
            self.choice=choice
            self.assertEqual('sent',await self.engine.handle(event(mid=i+1,text='发个表情包')))
        self.assertEqual([],self.images);self.assertEqual(7,len(self.texts))

    async def test_image_error_does_not_undo_sent_text_or_retry(self):
        async def fail(*_):raise TimeoutError()
        self.bridge.send=fail
        e=event(text='发个表情包')
        self.assertEqual('sent',await self.engine.handle(e));self.assertEqual('duplicate',await self.engine.handle(e))
        row=read_history(self.root,'90001','20001',frozenset(['20001']),errors_only=True)['rows'][0]
        self.assertEqual('sent',row['status']);self.assertEqual('uncertain',row['emoji_state']);self.assertEqual(1,len(self.texts))

    async def test_selection_error_sanitized_and_text_kept(self):
        async def fail(*_,**__):raise RuntimeError('API_SECRET PRIVATE_TEXT')
        self.bridge.model.generate=fail
        self.assertEqual('sent',await self.engine.handle(event(text='发个表情包')))
        row=read_history(self.root,'90001','20001',frozenset(['20001']))['rows'][0]
        self.assertEqual('RuntimeError',row['emoji_error']);self.assertNotIn('API_SECRET',str(row));self.assertEqual('sent',row['status'])

    async def test_missing_or_unavailable_assets_do_not_break_text(self):
        self.library.return_value={'success':True,'emojis':[]}
        self.assertEqual('sent',await self.engine.handle(event(text='发个表情包')))
        self.assertEqual([],self.images)
        self.bridge.helpers=None
        self.assertEqual('sent',await self.engine.handle(event(mid=2,text='发个表情包')))

    async def test_unknown_and_legacy_users_cannot_trigger_emoji(self):
        for uid in ['10001','30001']:
            self.assertEqual('ignored',await self.engine.handle(event(uid,text='发个表情包')))
        self.library.assert_not_awaited();self.assertEqual([],self.images)

    async def test_feature_disabled_for_other_personas(self):
        self.bridge.policies={'20001':EmojiPolicy(enabled=True)}
        self.assertEqual('sent',await self.engine.handle(event('20002',text='发个表情包')))
        self.library.assert_not_awaited();self.assertEqual([],self.images)

    async def test_concurrent_context_and_recipients_never_mix(self):
        await asyncio.gather(self.engine.handle(event('20001',1,'A_SECRET 发个表情包')),
                             self.engine.handle(event('20002',2,'B_SECRET 发个表情包')))
        self.assertEqual({'20001','20002'},{uid for uid,_ in self.images})
        for messages in self.selection_prompts:
            text=messages[1]['content']
            if 'A_SECRET' in text:self.assertNotIn('B_SECRET',text);self.assertNotIn('B_REPLY',text)
            else:self.assertNotIn('A_SECRET',text);self.assertNotIn('A_REPLY',text)

    async def test_automatic_cooldown_is_persistent_and_per_user(self):
        await self.engine.handle(event('20001',1,'好开心'))
        self.assertGreater(self.store().emoji_last_sent(),0)
        self.assertEqual(0,self.store('20002').emoji_last_sent())
        await self.engine.handle(event('20001',2,'不错'))
        self.assertEqual(1,len(self.images))
        await self.engine.handle(event('20001',3,'再来个表情包'))
        self.assertEqual(2,len(self.images))

    async def test_opt_out_persists_until_explicit_request(self):
        await self.engine.handle(event(mid=1,text='以后不要发表情包'))
        self.assertFalse(self.store().emoji_allowed())
        await self.engine.handle(event(mid=2,text='好开心'))
        self.assertEqual([],self.images)
        await self.engine.handle(event(mid=3,text='可以发个表情包了'))
        self.assertTrue(self.store().emoji_allowed());self.assertEqual(1,len(self.images))

    async def test_ack_silence_does_not_send_an_emoji(self):
        s=self.store();s.claim(Turn('20001','1','先忙'));s.finish('1','好，先忙吧。','sent')
        self.engine.policies={'20001':ReplyPolicy(enabled=True,allow_ack_silence=True)}
        self.assertEqual('no_reply',await self.engine.handle(event(mid=2,text='行')))
        self.library.assert_not_awaited();self.assertEqual([],self.images)

    async def test_cancellation_after_text_preserves_delivery(self):
        async def cancel(*_):raise asyncio.CancelledError()
        self.bridge.send=cancel
        self.assertEqual('sent',await self.engine.handle(event(text='发个表情包')))
        row=read_history(self.root,'90001','20001',frozenset(['20001']))['rows'][0]
        self.assertEqual('sent',row['status']);self.assertEqual('uncertain',row['emoji_state'])

    async def test_revoked_boundary_prevents_image(self):
        checks=[]
        def check():
            checks.append(1)
            if len(checks)>1:raise ValueError('revoked')
        self.bridge.boundary=check
        self.assertEqual('sent',await self.engine.handle(event(text='发个表情包')))
        self.assertEqual([],self.images)

    async def test_no_base64_or_foreign_emoji_in_history_projection(self):
        await self.engine.handle(event(text='发个表情包'))
        result=read_history(self.root,'90001','20001',frozenset(['20001']))
        self.assertEqual('sent',result['rows'][0]['emoji_state']);self.assertNotIn(png(),str(result))
        self.assertIn('已发送表情包',str(self.store().context('表情','999')))
        self.assertNotIn('已发送表情包',str(self.store('20002').context('表情','999')))

    async def test_corrupt_or_external_image_is_never_sent(self):
        for value in ['file:///etc/passwd','https://example.com/x.png',base64.b64encode(b'not image').decode()]:
            with self.assertRaises(Exception):validated_image(value)
        self.library.return_value=[{'description':'bad','base64':'https://example.com/x.png'}]
        await self.engine.handle(event(text='发个表情包'));self.assertEqual([],self.images)

    async def test_selector_update_requires_reaudit(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'togawasakiko-desuwa_emoji-select-with-text';p.mkdir()
            for name in self.helpers.hashes:shutil.copyfile(self.helpers.root/name,p/name)
            (p/'select_emoji.prompt').write_text('changed')
            with self.assertRaises(ValueError):SelectorHelpers(Path(d),self.helpers.hashes)

    async def test_onebot_image_payload_and_recipient_checks(self):
        c=Credentials('ws://localhost','fake','https://api.deepseek.com/chat/completions','fake','mock')
        client=OneBotTextClient(c,'90001',frozenset(['20001']),None,lambda:None,Mock());client.ready=True
        client.action=AsyncMock(return_value={'message_id':1})
        self.assertTrue(await client.send_emoji('20001',png()))
        args=client.action.call_args.args
        self.assertEqual('send_private_msg',args[0]);self.assertEqual(20001,args[1]['user_id'])
        segment=args[1]['message'][0];self.assertEqual('image',segment['type']);self.assertEqual(1,segment['data']['sub_type'])
        self.assertEqual('base64://'+png(),segment['data']['file'])
        with self.assertRaises(PermissionError):await client.send_emoji('20002',png())
        self.assertEqual(1,client.action.await_count)

    async def test_policy_and_request_detection(self):
        for bad in [dict(auto_probability=1),dict(cooldown_seconds=0),dict(enabled='yes'),dict(selection_timeout=100)]:
            with self.assertRaises(ValueError):EmojiPolicy.parse(bad)
        self.assertEqual('off',request_kind('不要给我发表情包'))
        self.assertEqual('explicit',request_kind('发个无奈的表情包'))


if __name__=='__main__':unittest.main(verbosity=2)
