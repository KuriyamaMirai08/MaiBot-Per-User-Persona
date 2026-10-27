"""MaiBot 1.3.15 isolated secondary personas and emoji bridge; legacy remains untouched."""
from pathlib import Path
from typing import ClassVar

import asyncio
import json

from maibot_sdk import API, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookOrder

from .boundary import PrimaryBoundary
from .isolation import IsolatedEngine
from .history_view import read_history
from .reply_policy import load_policies
from .emoji_bridge import EmojiBridge, SelectorHelpers, load_settings
from .routing import Router, qq_id
from .transport import Credentials, DirectModel, OneBotTextClient


class PluginSettings(PluginConfigBase):
    config_version: str = "0.2.3"
    enabled: bool = False


class RouterSettings(PluginConfigBase):
    account_id: str = ""
    legacy_user_ids: list[str] = Field(default_factory=list)
    authorized_user_ids: list[str] = Field(default_factory=list)
    secondary_privacy_lock: bool = True
    primary_policy_sha256: str = ""
    adapter_filters_sha256: str = ""


class Settings(PluginConfigBase):
    plugin: PluginSettings = Field(default_factory=PluginSettings)
    router: RouterSettings = Field(default_factory=RouterSettings)


class PerUserPersona(MaiBotPlugin):
    config_model: ClassVar[type[PluginConfigBase]] = Settings

    async def on_load(self):
        self.task = None
        self.client = None
        self.engine = None
        self.settings = self.get_plugin_config_data()["router"]
        data = await asyncio.to_thread(lambda: json.loads(Path(__file__).with_name("personas.json").read_text(encoding="utf-8")))
        self.router = Router.build(self.settings["account_id"], self.settings["legacy_user_ids"], data)
        authorized = frozenset(qq_id(x) for x in self.settings["authorized_user_ids"])
        self.authorized = authorized
        self.history_root = self.ctx.paths.data_dir / "isolated-v2" if authorized else None
        policies = await asyncio.to_thread(load_policies, Path(__file__).with_name("reply_policy.json"), self.router.personas)
        root = Path(__file__).resolve().parents[2]
        emoji_hashes, emoji_policies = await asyncio.to_thread(load_settings, Path(__file__).with_name('emoji_policy.json'), self.router.personas)
        self.boundary = PrimaryBoundary(root, self.router, authorized, self.settings["primary_policy_sha256"], self.settings["adapter_filters_sha256"])
        await asyncio.to_thread(self.boundary.check)
        if authorized and not self.settings["secondary_privacy_lock"]:
            credentials = await asyncio.to_thread(Credentials.read, root)
            model = DirectModel(credentials)
            self.client = OneBotTextClient(credentials, self.router.account_id, authorized, self.handle_event, self.boundary.check, self.ctx.logger)
            self.engine = IsolatedEngine(self.ctx.paths.data_dir / "isolated-v2", self.router, authorized,
                                         self.boundary.check, model, self.client.send, policies=policies)
            if any(p.enabled for uid,p in emoji_policies.items() if uid in authorized):
                try:
                    helpers = await asyncio.to_thread(SelectorHelpers, root/'plugins', emoji_hashes)
                except (ValueError, OSError):
                    helpers = None
                    self.ctx.logger.warning('Isolated emoji selector unavailable; text remains active')
                engine = self.engine
                self.engine.emoji = EmojiBridge(emoji_policies, helpers, self.ctx.emoji.get_all, model,
                                               self.client.send_emoji, self.boundary.check, lambda:engine.active)
                if helpers is not None:
                    try:
                        count = len(await asyncio.wait_for(self.engine.emoji.catalog(), 10))
                        self.ctx.logger.info('Isolated emoji bridge ready; shared asset candidates=%d', count)
                    except Exception:
                        self.ctx.logger.warning('Isolated emoji library preflight failed; text remains active')
            self.task = asyncio.create_task(self.client.run())
        self.ctx.logger.info("per-user-persona 0.2.3 loaded; isolated emoji bridge + admin history; approved users=%d; transport active=%s",
                             len(authorized), self.task is not None)

    async def handle_event(self, event):
        return await self.engine.handle(event) if self.engine is not None else "ignored"

    @API("history_overview", description="Authenticated administrator read-only history overview", public=False)
    async def history_overview(self):
        return dict(chats=[dict(user_id=uid, name=self.router.personas[uid].name) for uid in sorted(self.authorized)],
                    ready=bool(self.client is not None and self.client.ready), version="0.2.3",
                    emoji_available=bool(self.engine is not None and self.engine.emoji is not None and self.engine.emoji.helpers is not None))

    @API("history_page", description="Authenticated administrator read-only tenant history", public=False)
    async def history_page(self, user_id: str, before: int = 0, errors_only: bool = False):
        # The official WebUI gateway authenticates before dispatch; private static
        # API declarations prevent other plugins from calling these projections.
        if user_id not in self.authorized or self.history_root is None:
            raise PermissionError("Isolated chat not authorized")
        return await asyncio.to_thread(read_history, self.history_root, self.router.account_id,
                                       user_id, self.authorized, before=before, errors_only=errors_only)

    async def on_unload(self):
        if self.engine is not None: self.engine.active = False
        if self.client is not None: await self.client.stop()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def on_config_update(self, scope: str, config_data: dict, version: str):
        await self.on_unload()
        # A failed new configuration leaves isolated transport stopped.
        self.set_plugin_config(config_data)
        await self.on_load()

    @HookHandler("chat.receive.before_process", name="persona_ingress_guard", order=HookOrder.EARLY, error_policy=ErrorPolicy.ABORT)
    async def guard_receive(self, **kwargs):
        return {"action": "abort" if self.router.decision(kwargs.get("message")) == "block" else "continue"}

    @HookHandler("send_service.before_send", name="persona_egress_guard", order=HookOrder.EARLY, error_policy=ErrorPolicy.ABORT)
    async def guard_send(self, **kwargs):
        return {"action": "abort" if self.router.decision(kwargs.get("message"), outgoing=True) == "block" else "continue"}


def create_plugin():
    return PerUserPersona()
