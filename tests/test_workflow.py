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


async def test_manual_retry_and_multiple_images_without_quota(tmp_path, story, messages, png):
    store, drawing, flow = setup(tmp_path, story, png, True)
    draft = await flow.draft("g", "a", messages)
    with pytest.raises(TheaterError):
        await flow.render(draft["id"], "g", "a")
    assert drawing.calls == 1
    assert store.get(draft["id"], "g", "a")["status"] == "failed"
    drawing.fail = False
    await flow.render(draft["id"], "g", "a")
    for _ in range(3):
        another = await flow.draft("other_group", "b", messages)
        await flow.render(another["id"], "other_group", "b")
    assert drawing.calls == 5
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
    assert store.get(draft["id"], "g", "a")["status"] == "draft" and drawing.calls == 0
    store.close()


def test_restart_does_not_resubmit_but_allows_manual_retry(tmp_path, story, messages):
    now = [1000.0]
    store = Store(tmp_path, lambda: now[0])
    draft = store.create("g", "a", [], validate_story(story, messages), 86400)
    store.reserve(draft)
    store.close()
    store = Store(tmp_path, lambda: now[0])
    assert store.get(draft["id"], "g", "a")["status"] == "unknown"
    store.reserve(store.get(draft["id"], "g", "a"))
    assert store.get(draft["id"], "g", "a")["status"] == "rendering"
    store.close()


def test_expiry_deletes_source(tmp_path, story, messages):
    now = [1000.0]
    store = Store(tmp_path, lambda: now[0])
    draft = store.create("g", "a", [], validate_story(story, messages), 10)
    store.reserve(draft)
    store.finish(draft["id"], "failed")
    now[0] += 11
    with pytest.raises(TheaterError, match="过期"):
        store.get(draft["id"], "g", "a")
    assert store.cleanup() == 1
    store.close()


def test_revision_change_blocks_old_confirmation(tmp_path, story, messages):
    store = Store(tmp_path)
    clean = validate_story(story, messages)
    draft = store.create("g", "a", [], clean, 3600)
    store.revise(draft, clean)
    with pytest.raises(TheaterError, match="修改"):
        store.reserve(draft)
    store.close()


@pytest.mark.parametrize(
    "key,value", [("candidate_count", 0), ("retention_hours", -1), ("retention_hours", "oops")]
)
def test_invalid_limits_fail_closed(key, value):
    with pytest.raises(TheaterError):
        Workflow(None, None, None, {key: value})
