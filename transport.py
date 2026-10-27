"""Direct stateless DeepSeek + OneBot text transport, avoiding Host memory/cache.

No host send/LLM/message/database capability is used. Tokens stay in memory and
HTTP headers; errors and logs must never contain URL queries or response bodies.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

import asyncio
import json
import tomllib

import aiohttp

from .reply_policy import ModelFailure, ModelReply


@dataclass(frozen=True)
class Credentials:
    ws_url: str
    ws_token: str = field(repr=False)
    api_url: str
    api_key: str = field(repr=False)
    model: str

    @classmethod
    def read(cls, root: Path):
        adapter = tomllib.loads((root / "plugins/MaiBot-SnowLuma-Adapter/config.toml").read_text())
        client = adapter["client"]
        if client["client_type"] != "napcat" or client["server"] != "host.docker.internal":
            raise ValueError("Unverified OneBot endpoint")
        if type(client["port"]) is not int or client["port"] != 18080 or not client.get("token"):
            raise ValueError("Unverified OneBot port/authentication")
        models = tomllib.loads((root / "config/model_config.toml").read_text())
        candidates = models["model_task_config"]["replyer"]["model_list"]
        if len(candidates) != 1:
            raise ValueError("Exactly one audited reply model required")
        model = next(x for x in models["models"] if x["name"] == candidates[0])
        provider = next(x for x in models["api_providers"] if x["name"] == model["api_provider"])
        base = provider["base_url"].rstrip("/")
        if base not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"} or not provider.get("api_key"):
            raise ValueError("Only existing official DeepSeek provider supported")
        return cls(f"ws://{client['server']}:{client['port']}", client["token"],
                   base + "/chat/completions", provider["api_key"], model["model_identifier"])


class DirectModel:
    def __init__(self, credentials: Credentials):
        self.credentials = credentials

    async def __call__(self, messages: list[dict]) -> str:
        return (await self.generate(messages)).text

    async def generate(self, messages: list[dict], *, temperature=None, max_tokens=2048, json_object=False) -> ModelReply:
        c = self.credentials
        # New, stateless request every time. No Host prompt-cache statistics,
        # conversation IDs, tools, retries, filesystem or memory service calls.
        payload = {"model": c.model, "messages": messages, "stream": False, "max_tokens": max_tokens}
        if temperature is not None:
            payload["temperature"] = temperature
        if json_object:
            payload["response_format"] = {"type": "json_object"}
            # Structured selection needs an index, not a reasoning budget. This
            # is request-local and leaves ordinary persona text calls unchanged.
            payload["thinking"] = {"type": "disabled"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=100), trust_env=False) as session:
            async with session.post(c.api_url, headers={"Authorization": "Bearer " + c.api_key},
                                    json=payload,
                                    allow_redirects=False) as response:
                if response.status != 200:
                    raise ModelFailure(f"model_http_{response.status}")
                data = await response.json()
        answer = data["choices"][0]["message"].get("content")
        if not isinstance(answer, str):
            raise ValueError("Missing isolated model response")
        return ModelReply(answer, c.model, str(data["choices"][0].get("finish_reason", "")))


class OneBotTextClient:
    def __init__(self, credentials: Credentials, account_id: str, authorized: frozenset[str],
                 handler: Callable[[dict], Awaitable[str]], check_boundary: Callable[[], None], logger):
        self.credentials, self.account_id, self.authorized = credentials, account_id, authorized
        self.handler, self.boundary, self.logger = handler, check_boundary, logger
        self.pending = {}
        self.ws = None
        self.ready = False
        self.jobs = set()
        self.stopping = False

    async def action(self, action: str, params: dict):
        if self.ws is None or self.ws.closed:
            raise ConnectionError("OneBot unavailable")
        echo = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await self.ws.send_json({"action": action, "params": params, "echo": echo})
            response = await asyncio.wait_for(future, 15)
            if response.get("status") != "ok" or response.get("retcode") != 0:
                raise RuntimeError("OneBot action rejected")
            return response.get("data", {})
        finally:
            self.pending.pop(echo, None)

    async def send(self, user_id: str, text: str) -> bool:
        await asyncio.to_thread(self.boundary)
        if user_id not in self.authorized or not self.ready or self.stopping:
            raise PermissionError("Unapproved isolated recipient")
        result = await self.action("send_private_msg", {"user_id": int(user_id),
                                   "message": [{"type": "text", "data": {"text": text}}]})
        return bool(result.get("message_id"))

    async def send_emoji(self, user_id: str, base64_data: str) -> bool:
        # No external URLs or filesystem paths are accepted, even if a selector
        # or model misbehaves. Recipient is the same approved transport identity.
        from .emoji_bridge import validated_image
        await asyncio.to_thread(self.boundary)
        if user_id not in self.authorized or not self.ready or self.stopping:
            raise PermissionError('Unapproved isolated emoji recipient')
        await asyncio.to_thread(validated_image, base64_data)
        result = await self.action('send_private_msg', {'user_id':int(user_id),'message':[
            {'type':'image','data':{'file':'base64://'+base64_data,'sub_type':1}}]})
        return bool(result.get('message_id'))

    async def _job(self, event):
        try:
            await self.handler(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not log exceptions: upstream errors may contain private data.
            self.logger.warning("Isolated turn failed; no automatic resend")

    async def receive(self):
        async for frame in self.ws:
            if frame.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                event = json.loads(frame.data)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            echo = event.get("echo")
            if isinstance(echo, str) and echo in self.pending:
                future = self.pending[echo]
                if not future.done():
                    future.set_result(event)
                continue
            # Drop all legacy/unauthorized events before task or cache creation.
            if self.ready and str(event.get("user_id", "")) in self.authorized and len(self.jobs) < 32:
                task = asyncio.create_task(self._job(event))
                self.jobs.add(task)
                task.add_done_callback(self.jobs.discard)

    async def run(self):
        while not self.stopping:
            reader = None
            try:
                await asyncio.to_thread(self.boundary)
                async with aiohttp.ClientSession(trust_env=False) as session:
                    async with session.ws_connect(self.credentials.ws_url, headers={"Authorization": "Bearer " + self.credentials.ws_token},
                                                  heartbeat=20, max_msg_size=2 * 1024 * 1024) as ws:
                        self.ws = ws
                        reader = asyncio.create_task(self.receive())
                        login = await self.action("get_login_info", {})
                        if str(login.get("user_id")) != self.account_id:
                            raise ValueError("Wrong QQ account")
                        self.ready = True
                        self.logger.info("Isolated text transport ready; QQ account verified")
                        await reader
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.warning("Isolated transport unavailable; original adapter unaffected")
            finally:
                self.ready = False
                if reader is not None:
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                for future in list(self.pending.values()):
                    if not future.done(): future.set_exception(ConnectionError("OneBot disconnected"))
                self.ws = None
                # Old turns must not send through a new socket after reconnect.
                jobs = list(self.jobs)
                for task in jobs: task.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
            if not self.stopping:
                await asyncio.sleep(5)

    async def stop(self):
        self.stopping = True
        self.ready = False
        for task in list(self.jobs): task.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        if self.ws is not None: await self.ws.close()
