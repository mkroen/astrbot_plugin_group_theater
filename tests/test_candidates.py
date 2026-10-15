import copy
import json

import pytest

from core.store import Store
from core.story import TheaterError, candidate_card, validate_candidates
from core.workflow import Workflow


def batch(story):
    result = []
    for index in range(3):
        item = copy.deepcopy(story)
        item["title"] = f"剧场{index}"
        item["panels"][3]["dialogue"][0]["text"] = f"反转{index}"
        result.append(item)
    return result


async def test_one_text_call_three_saved_candidates_select_one(tmp_path, story, messages, png):
    requests, draws = [], []

    async def text(prompt):
        requests.append(json.loads(prompt))
        return json.dumps({"candidates": batch(story)})

    class Drawing:
        config = {}

        def validate(self):
            pass

        async def generate(self, prompt, refs):
            draws.append(prompt)
            return png

    store = Store(tmp_path)
    flow = Workflow(store, text, Drawing(), {"use_avatars": False})
    drafts = await flow.candidates("g", "a", messages)
    assert len(drafts) == 3 and len({d["id"] for d in drafts}) == 3
    assert len(requests) == 1 and requests[0]["candidate_count"] == 3
    assert draws == []
    await flow.render(drafts[1]["id"], "g", "a")
    assert len(requests) == 1 and len(draws) == 1 and "反转1" in draws[0]
    assert store.get(drafts[0]["id"], "g", "a")["status"] == "draft"
    await flow.render(drafts[1]["id"], "g", "a")
    assert len(draws) == 1
    store.close()


def test_invalid_and_duplicate_candidates_filtered(story, messages):
    bad = copy.deepcopy(story)
    bad["source_ids"] = ["missing"]
    duplicate = copy.deepcopy(story)
    duplicate["title"] = "换个标题"
    result = validate_candidates(json.dumps({"candidates": [story, bad, duplicate]}), messages, 3)
    assert len(result) == 1


@pytest.mark.parametrize("raw", ["invalid", "{}", '{"candidates":[]}', '{"candidates":[{}]}'])
def test_no_valid_candidates_fail_without_images(raw, messages):
    with pytest.raises(TheaterError):
        validate_candidates(raw, messages, 3)


def test_card_is_compact_and_complete(story, messages):
    from core.story import validate_story

    story = validate_story(story, messages)
    story["premise"] = "很长的背景" * 50
    for panel in story["panels"]:
        panel["action"] = "很长的动作" * 50
    card = candidate_card({"id": "abcdef123456", "story": story}, 2)
    assert len(card) < 650
    assert "候选 2" in card and "ID：abcdef123456" in card
    for beat in ["铺垫", "推进", "转折", "包袱"]:
        assert beat in card
    assert "/小剧场 出图 abcdef123456" in card and "改稿" not in card


async def test_batch_write_rolls_back_on_failure(tmp_path, story, messages, monkeypatch):
    store = Store(tmp_path)

    async def text(prompt):
        return json.dumps({"candidates": batch(story)})

    original = store.create
    calls = []

    def failing(*args):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("write failure")
        return original(*args)

    monkeypatch.setattr(store, "create", failing)
    with pytest.raises(RuntimeError):
        await Workflow(store, text, None, {}).candidates("g", "a", messages)
    assert store.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0
    store.close()
