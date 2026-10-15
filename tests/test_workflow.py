import asyncio
import json

import pytest

from core.store import Store
from core.story import TheaterError, validate_story
from core.workflow import Workflow


class FakeDrawing:
    config = {"protocol": "openai_chat"}

    def __init__(self, png, fail=False):
        self.calls = 0
        self.avatars = []
        self.png = png
        self.fail = fail

    def validate(self):
        pass

    async def avatar(self, uid):
        self.avatars.append(uid)
        return self.png

    async def generate(self, prompt, refs):
        self.calls += 1
        await asyncio.sleep(0)
        if self.fail:
            raise TheaterError("timeout")
        return self.png


def setup(tmp_path, story, png, fail=False):
    store = Store(tmp_path)
    drawing = FakeDrawing(png, fail)

    async def text(prompt):
        return json.dumps(story)

    workflow = Workflow(store, text, drawing, {"use_avatars": True})
    return store, drawing, workflow


async def test_draft_revision_never_draw_and_replay_cached(tmp_path, story, messages, png):
    store, drawing, flow = setup(tmp_path, story, png)
    draft = await flow.draft("group1", "admin", messages)
    assert drawing.calls == 0 and drawing.avatars == []
    draft = await flow.revise(draft["id"], "group1", "admin", "更短")
    assert draft["revision"] == 2 and drawing.calls == 0
    path = await flow.render(draft["id"], "group1", "admin")
    assert path.read_bytes() == png and drawing.calls == 1
    assert drawing.avatars == ["10001", "10002"]
    assert await flow.render(draft["id"], "group1", "admin") == path
    assert drawing.calls == 1
    store.close()


@pytest.mark.parametrize("scope,owner", [("group2", "admin"), ("group1", "another")])
async def test_cannot_render_other_group_or_owner(tmp_path, story, messages, png, scope, owner):
    store, drawing, flow = setup(tmp_path, story, png)
    draft = await flow.draft("group1", "admin", messages)
    with pytest.raises(TheaterError):
        await flow.render(draft["id"], scope, owner)
    assert drawing.calls == 0
    store.close()


async def test_failure_consumes_quota_and_never_retries(tmp_path, story, messages, png):
    store, drawing, flow = setup(tmp_path, story, png, True)
    draft = await flow.draft("g", "a", messages)
    with pytest.raises(TheaterError):
        await flow.render(draft["id"], "g", "a")
    assert store.quota(18000) == 1
    with pytest.raises(TheaterError):
        await flow.render(draft["id"], "g", "a")
    second = await flow.draft("other_group", "b", messages)
    with pytest.raises(TheaterError, match="额度"):
        await flow.render(second["id"], "other_group", "b")
    assert drawing.calls == 1
    store.close()


async def test_duplicate_confirm_race_submits_once(tmp_path, story, messages, png):
    store, drawing, flow = setup(tmp_path, story, png)
    draft = await flow.draft("g", "a", messages)
    await asyncio.gather(
        flow.render(draft["id"], "g", "a"),
        flow.render(draft["id"], "g", "a"),
        return_exceptions=True,
    )
    assert drawing.calls == 1
    store.close()


async def test_bad_avatar_aborts_before_reservation(tmp_path, story, messages, png):
    store, drawing, flow = setup(tmp_path, story, png)

    async def bad(uid):
        raise ValueError("download failed")

    drawing.avatar = bad
    draft = await flow.draft("g", "a", messages)
    with pytest.raises(TheaterError, match="头像"):
        await flow.render(draft["id"], "g", "a")
    assert store.quota(18000) == 0 and drawing.calls == 0
    store.close()


def test_restart_does_not_resubmit_and_quota_persists(tmp_path, story, messages):
    now = [1000.0]
    store = Store(tmp_path, lambda: now[0])
    draft = store.create("g", "a", [], validate_story(story, messages), 86400)
    store.reserve(draft, 1, 18000)
    store.close()
    store = Store(tmp_path, lambda: now[0])
    assert store.get(draft["id"], "g", "a")["status"] == "unknown"
    assert store.quota(18000) == 1
    now[0] += 18001
    assert store.quota(18000) == 0
    store.close()


def test_expiry_deletes_source_but_does_not_refund_quota(tmp_path, story, messages):
    now = [1000.0]
    store = Store(tmp_path, lambda: now[0])
    draft = store.create("g", "a", [], validate_story(story, messages), 10)
    store.reserve(draft, 1, 18000)
    store.finish(draft["id"], "failed")
    now[0] += 11
    with pytest.raises(TheaterError, match="过期"):
        store.get(draft["id"], "g", "a")
    assert store.cleanup() == 1 and store.quota(18000) == 1
    store.close()


def test_revision_change_blocks_old_confirmation(tmp_path, story, messages):
    store = Store(tmp_path)
    clean = validate_story(story, messages)
    draft = store.create("g", "a", [], clean, 3600)
    store.revise(draft, clean)
    with pytest.raises(TheaterError, match="修改"):
        store.reserve(draft, 1, 18000)
    assert store.quota(18000) == 0
    store.close()


@pytest.mark.parametrize(
    "key,value", [("image_window_hours", 0), ("image_limit", -1), ("retention_hours", "oops")]
)
def test_invalid_limits_fail_closed(key, value):
    with pytest.raises(TheaterError):
        Workflow(None, None, None, {key: value})
