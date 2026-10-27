import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from aiohttp import web
import test_emoji_bridge as base
from test_isolation import event
from isolated_test_pkg.emoji_bridge import selection_index, SelectionFailure
from isolated_test_pkg.reply_policy import ModelReply
from isolated_test_pkg.transport import Credentials, DirectModel


class SelectionReliabilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.EmojiTests.asyncSetUp
    asyncTearDown = base.EmojiTests.asyncTearDown
    store = base.EmojiTests.store

    def test_single_json_and_code_fences_are_supported(self):
        for value in ['{"selected":1}', '```json\n{"selected":1}\n```', '```\n{"selected":1}\n```', '\ufeff{"selected":1}']:
            self.assertEqual(1, selection_index(ModelReply(value, 'mock', 'stop'), 2))
        self.assertEqual(0, selection_index(ModelReply('{"selected":0}'), 2))

    def test_invalid_or_ambiguous_output_is_not_salvaged(self):
        for value in ['', 'hello {"selected":1}', '{"selected":1}{"selected":2}',
                      '{"selected":1,"selected":2}', '{"selected":true}', '{"selected":1.5}',
                      '{"selected":-1}', '{"selected":3}', '{"selected":1,"extra":0}', '[1]', 'null']:
            with self.assertRaises(SelectionFailure): selection_index(ModelReply(value), 2)
        with self.assertRaisesRegex(SelectionFailure, 'selection_truncated'):
            selection_index(ModelReply('{"selected":1}', 'mock', 'length'), 2)

    async def test_empty_result_retries_generation_once_without_extra_send(self):
        self.bridge.model.generate = AsyncMock(side_effect=[ModelReply('', 'mock', 'stop'), ModelReply('{"selected":1}', 'mock', 'stop')])
        self.assertEqual('sent', await self.engine.handle(event(text='发个表情包')))
        self.assertEqual(2, self.bridge.model.generate.await_count)
        self.assertEqual(1, len(self.texts));self.assertEqual(1, len(self.images))
        calls = self.bridge.model.generate.call_args_list
        self.assertEqual([256,512], [c.kwargs['max_tokens'] for c in calls])
        self.assertTrue(all(c.kwargs['json_object'] for c in calls))
        self.assertEqual(calls[0].args[0][1], calls[1].args[0][1])

    async def test_truncated_result_is_not_used_even_when_index_looks_valid(self):
        self.bridge.model.generate = AsyncMock(side_effect=[ModelReply('{"selected":2}', 'mock', 'length'), ModelReply('{"selected":1}', 'mock', 'stop')])
        await self.engine.handle(event(text='发个表情包'))
        self.assertEqual(2, self.bridge.model.generate.await_count);self.assertEqual(1, len(self.images))
        self.assertIn('微笑', self.store().context('表情','999')[-1]['content'])

    async def test_persistent_empty_result_fails_clearly_and_does_not_retry_send(self):
        self.bridge.model.generate = AsyncMock(return_value=ModelReply('', 'mock', 'length'))
        e=event(text='发个表情包')
        self.assertEqual('sent', await self.engine.handle(e))
        self.assertEqual('duplicate', await self.engine.handle(e))
        self.assertEqual(2, self.bridge.model.generate.await_count);self.assertEqual([], self.images)
        row=base.read_history(self.root,'90001','20001',frozenset(['20001']))['rows'][0]
        self.assertEqual('selection_truncated', row['emoji_error']);self.assertEqual('sent', row['status'])

    async def test_invalid_recipient_index_does_not_trigger_retry_or_fallback(self):
        self.bridge.model.generate=AsyncMock(return_value=ModelReply('{"selected":999}'))
        await self.engine.handle(event(text='发个表情包'))
        self.assertEqual(1,self.bridge.model.generate.await_count);self.assertEqual([],self.images)

    async def test_explicit_request_bypasses_probability_gate(self):
        self.bridge.random_draw=lambda:1
        await self.engine.handle(event(mid=1,text='好开心'))
        self.assertEqual([],self.images)
        await self.engine.handle(event(mid=2,text='发个开心的表情包'))
        self.assertEqual(1,len(self.images))


class ModelRequestContract(unittest.IsolatedAsyncioTestCase):
    async def test_json_mode_and_thinking_override_are_selection_local(self):
        captured=[]
        async def handler(request):
            captured.append(await request.json())
            return web.json_response({'choices':[{'message':{'content':'{"selected":1}'},'finish_reason':'stop'}]})
        app=web.Application();app.router.add_post('/',handler)
        runner=web.AppRunner(app);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        model=DirectModel(Credentials('ws://unused','fake',f'http://127.0.0.1:{port}/','test-only','mock'))
        try:
            messages=[{'role':'user','content':'test JSON'}]
            await model.generate(messages,max_tokens=256,json_object=True)
            await model.generate(messages,max_tokens=96,temperature=.65)
            self.assertEqual({'type':'json_object'},captured[0]['response_format'])
            self.assertEqual({'type':'disabled'},captured[0]['thinking'])
            self.assertNotIn('response_format',captured[1]);self.assertNotIn('thinking',captured[1])
            self.assertEqual(.65,captured[1]['temperature']);self.assertEqual(96,captured[1]['max_tokens'])
        finally:await runner.cleanup()
