from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import asyncio
import json
import unittest

import aiohttp
from aiohttp import web

from test_isolation import event, router
from isolated_test_pkg.isolation import IsolatedEngine
from isolated_test_pkg.transport import Credentials, DirectModel, OneBotTextClient


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_websocket_uses_echo_and_exact_recipient(self):
        import tempfile
        from pathlib import Path
        received = []; done = asyncio.Event(); prompts = []
        async def server(request):
            self.assertEqual('Bearer test-only', request.headers.get('Authorization'))
            ws = web.WebSocketResponse(); await ws.prepare(request)
            async for msg in ws:
                payload = json.loads(msg.data); received.append(payload)
                if payload['action'] == 'get_login_info':
                    await ws.send_json(dict(status='ok',retcode=0,echo=payload['echo'],data=dict(user_id=90001)))
                    # Yield login to client, then two concurrent friends and an intruder.
                    await asyncio.sleep(0.03)
                    for e in [event('20001',1,'A_SECRET'), event('20002',2,'B_SECRET'), event('30001',3,'UNAUTHORIZED'),event('10001',4,'ADMIN_SECRET')]:
                        await ws.send_json(e)
                elif payload['action'] == 'send_private_msg':
                    await ws.send_json(dict(status='ok',retcode=0,echo=payload['echo'],data=dict(message_id=len(received))))
                    if len([x for x in received if x['action']=='send_private_msg'])==2: done.set()
                else: self.fail('Unexpected OneBot action')
            return ws
        app = web.Application(); app.router.add_get('/',server)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner,'127.0.0.1',0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        credentials = Credentials(f'http://127.0.0.1:{port}/','test-only','https://api.deepseek.com/chat/completions','not-a-key','mock')
        async def model(messages):
            prompts.append(messages); await asyncio.sleep(0)
            return 'A response' if 'Persona A' in messages[0]['content'] else 'B response'
        with tempfile.TemporaryDirectory() as tmp:
            engine = None
            async def handler(e): return await engine.handle(e)
            client = OneBotTextClient(credentials,'90001',frozenset(['20001','20002']),handler,lambda:None,Mock())
            engine = IsolatedEngine(Path(tmp),router(),frozenset(['20001','20002']),lambda:None,model,client.send)
            task = asyncio.create_task(client.run())
            try:
                await asyncio.wait_for(done.wait(),5)
                await asyncio.sleep(0.05)
                sends=[x['params'] for x in received if x['action']=='send_private_msg']
                self.assertEqual({20001,20002},{x['user_id'] for x in sends})
                for msg in sends:
                    self.assertEqual('A response' if msg['user_id']==20001 else 'B response',msg['message'][0]['data']['text'])
                self.assertNotIn('ADMIN_SECRET',str(prompts));self.assertNotIn('UNAUTHORIZED',str(prompts))
                self.assertEqual(3,len({x['echo'] for x in received}))
            finally:
                engine.active=False;await client.stop();task.cancel();await asyncio.gather(task,return_exceptions=True)
        await runner.cleanup()

    async def test_unapproved_raw_send_is_refused(self):
        c=Credentials('ws://localhost','test','https://api.deepseek.com/chat/completions','fake','mock')
        client=OneBotTextClient(c,'90001',frozenset(['20001']),None,lambda:None,Mock())
        client.ready=True
        with self.assertRaises(PermissionError):await client.send('20002','bad')

    async def test_secret_credentials_not_in_repr(self):
        c=Credentials('ws://localhost','WS_SECRET','https://api.deepseek.com/chat/completions','API_SECRET','mock')
        self.assertNotIn('WS_SECRET',repr(c));self.assertNotIn('API_SECRET',repr(c))

    async def test_reverse_echo_replies_are_correlated(self):
        c=Credentials('ws://localhost','test','https://api.deepseek.com/chat/completions','fake','mock')
        client=OneBotTextClient(c,'90001',frozenset(['20001','20002']),None,lambda:None,Mock())
        payloads=[]
        class Socket:
            closed=False
            async def send_json(self,payload):
                payloads.append(payload)
                if len(payloads)==2:
                    for p in reversed(payloads):
                        client.pending[p['echo']].set_result(dict(status='ok',retcode=0,data=dict(message_id=p['params']['user_id'])))
        client.ws=Socket()
        a,b=await asyncio.gather(client.action('send_private_msg',{'user_id':20001}),client.action('send_private_msg',{'user_id':20002}))
        self.assertEqual(20001,a['message_id']);self.assertEqual(20002,b['message_id'])


if __name__ == '__main__': unittest.main(verbosity=2)
