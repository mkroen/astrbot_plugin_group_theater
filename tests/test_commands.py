"""Exercise the adapter offline; SDK signatures separately checked against AstrBot 4.28.2."""

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def plugin_class(monkeypatch, tmp_path):
    def decorator(*args, **kwargs):
        return lambda func: func

    class Star:
        def __init__(self, context):
            self.context = context

    exports = {
        "astrbot": {},
        "astrbot.api": {"logger": SimpleNamespace(error=lambda *a: None)},
        "astrbot.api.event": {
            "AstrMessageEvent": object,
            "filter": SimpleNamespace(
                command=decorator,
                permission_type=decorator,
                PermissionType=SimpleNamespace(ADMIN="admin"),
            ),
        },
        "astrbot.api.message_components": {
            "Image": SimpleNamespace(fromFileSystem=lambda p: p),
            "Plain": lambda text: SimpleNamespace(text=text),
            "Node": lambda **kw: SimpleNamespace(**kw),
            "Nodes": lambda nodes: SimpleNamespace(nodes=nodes),
        },
        "astrbot.api.star": {
            "Context": object,
            "Star": Star,
            "register": decorator,
            "StarTools": SimpleNamespace(get_data_dir=lambda _: tmp_path),
        },
        "astrbot.core": {},
        "astrbot.core.star": {},
        "astrbot.core.star.filter": {},
        "astrbot.core.star.filter.command": {"GreedyStr": str},
    }
    for name, attributes in exports.items():
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "theater_test_plugin", root / "main.py", submodule_search_locations=[str(root)]
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module.GroupTheaterPlugin


class Event:
    unified_msg_origin = "qq:GroupMessage:123"
    body = "/小剧场 编剧 甲：我很勇敢\n乙：那你先上"

    def get_group_id(self):
        return "123"

    def get_sender_id(self):
        return "456"

    def get_self_id(self):
        return "789"

    def get_platform_name(self):
        return "aiocqhttp"

    def get_message_str(self):
        return self.body

    def plain_result(self, text):
        return text

    def chain_result(self, parts):
        return parts

    def should_call_llm(self, value):
        self.llm = value

    def stop_event(self):
        self.stopped = True


async def test_disabled_and_help_do_not_call_models(plugin_class):
    plugin = plugin_class(SimpleNamespace(), {})
    event = Event()
    assert "未启用" in (await anext(plugin.theater(event, "取材")))
    assert "群友四格" in (await anext(plugin.theater(event, "")))
    assert event.llm is False and event.stopped
    await plugin.terminate()


async def test_history_returns_forward_candidates(plugin_class, story):
    calls = []

    async def llm_generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(completion_text=json.dumps({"candidates": [story]}))

    async def call_action(action, **kwargs):
        assert action == "get_group_msg_history" and kwargs["group_id"] == 123
        return {
            "messages": [
                {"sender": {"user_id": "1", "nickname": "甲"}, "message": "我很勇敢"},
                {"sender": {"user_id": "2", "nickname": "乙"}, "message": "那你先上"},
            ]
        }

    plugin = plugin_class(
        SimpleNamespace(llm_generate=llm_generate),
        {
            "enabled": True,
            "allowed_groups": ["123"],
            "story_provider_id": "mock",
        },
    )
    event = Event()
    event.bot = SimpleNamespace(api=SimpleNamespace(call_action=call_action))
    results = [item async for item in plugin.theater(event, "取材 10")]
    assert len(calls) == 1 and len(results[-1][0].nodes) == 2
    await plugin.terminate()


async def test_credential_reuse_without_key_rotation(plugin_class):
    context = SimpleNamespace(
        get_config=lambda: {
            "provider_sources": [
                {
                    "id": "test-source",
                    "key": ["test-first", "test-second"],
                    "api_base": "https://example.com/v1",
                }
            ]
        }
    )
    plugin = plugin_class(context, {"drawing": {"credential_source_id": "test-source"}})
    client = plugin._drawing()
    assert client.key == "test-first" and client.config["api_base"] == "https://example.com/v1"
    await plugin.terminate()


@pytest.mark.parametrize("allowed,permitted", [([], True), (["123"], True), (["999"], False)])
async def test_group_whitelist_empty_allows_all(plugin_class, allowed, permitted):
    plugin = plugin_class(SimpleNamespace(), {"enabled": True, "allowed_groups": allowed})
    results = [item async for item in plugin.theater(Event(), "分镜 missing")]
    assert ("找不到" in results[0]) is permitted
    if not permitted:
        assert "白名单" in results[0]
    await plugin.terminate()


async def test_all_groups_does_not_enable_private_chat(plugin_class):
    plugin = plugin_class(SimpleNamespace(), {"enabled": True})
    event = Event()
    event.get_group_id = lambda: ""
    results = [item async for item in plugin.theater(event, "分镜 missing")]
    assert "群聊" in results[0]
    await plugin.terminate()


@pytest.mark.parametrize("selected", ["", None, "fixed-model"])
async def test_text_model_default_or_explicit(plugin_class, selected):
    lookups, calls = [], []

    async def get_using_provider_async(**kwargs):
        lookups.append(kwargs["umo"])
        return SimpleNamespace(meta=lambda: SimpleNamespace(id="session-model"))

    async def llm_generate(**kwargs):
        calls.append(kwargs["chat_provider_id"])
        return SimpleNamespace(completion_text="mock response")

    context = SimpleNamespace(
        get_using_provider_async=get_using_provider_async, llm_generate=llm_generate
    )
    plugin = plugin_class(context, {"story_provider_id": selected})
    assert await plugin._generate_text(Event(), "test") == "mock response"
    assert calls == [selected or "session-model"]
    assert lookups == ([] if selected else [Event.unified_msg_origin])
    await plugin.terminate()


@pytest.mark.parametrize("command", ["编剧", "状态", "取消", "额度", "清理", "改稿"])
async def test_removed_commands_only_show_basic_help(plugin_class, command):
    plugin = plugin_class(SimpleNamespace(), {"enabled": True})
    replies = [item async for item in plugin.theater(Event(), command)]
    assert len(replies) == 1
    assert "/小剧场 取材" in replies[0]
    assert "/小剧场 分镜" in replies[0]
    assert "/小剧场 出图" in replies[0]
    assert "/小剧场 " + command not in replies[0]
    assert plugin.store.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0
    await plugin.terminate()
