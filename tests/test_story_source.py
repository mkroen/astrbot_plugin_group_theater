import copy
import json

import pytest

from core.source import normalize_history, pasted_messages
from core.story import TheaterError, drawing_prompt, story_prompt, validate_story


def test_model_never_receives_qq_ids(messages):
    prompt = story_prompt(messages)
    assert "10001" not in prompt and "user_id" not in prompt
    assert json.loads(prompt)["messages"][0]["speaker"] == "u1"


def test_valid_story_and_reference_mapping(story, messages):
    clean = validate_story(json.dumps(story), messages)
    assert clean["cast"][0]["name"] == "甲"
    prompt = drawing_prompt(clean, ["u2", "u1"], "四格")
    assert "u1 (甲): reference image 2" in prompt
    assert "u2 (乙): reference image 1" in prompt


@pytest.mark.parametrize(
    "mutation",
    ["five_panels", "invented_id", "invented_actor", "long_dialogue", "bad_beat", "bad_json"],
)
def test_invalid_script_cannot_reach_drawing(story, messages, mutation):
    if mutation == "five_panels":
        story["panels"].append(copy.deepcopy(story["panels"][0]))
    if mutation == "invented_id":
        story["source_ids"] = ["m666"]
    if mutation == "invented_actor":
        story["panels"][0]["dialogue"][0]["speaker"] = "u999"
    if mutation == "long_dialogue":
        story["panels"][0]["dialogue"][0]["text"] = "字" * 25
    if mutation == "bad_beat":
        story["panels"][0]["beat"] = "每日新闻"
    if mutation == "bad_json":
        story = "not json"
    with pytest.raises(TheaterError):
        validate_story(story, messages)


def test_history_filters_bot_commands_images_and_duplicates():
    rows = [
        {
            "time": 3,
            "message_id": 3,
            "sender": {"user_id": 2, "nickname": "乙"},
            "message": [{"type": "image", "data": {"url": "secret"}}],
        },
        {
            "time": 1,
            "message_id": 1,
            "sender": {"user_id": 1, "nickname": "甲"},
            "message": "你好[CQ:image,file=secret]",
        },
        {"time": 1, "message_id": 1, "sender": {"user_id": 1}, "message": "重复"},
        {"time": 2, "sender": {"user_id": 99}, "message": "机器人"},
        {"time": 4, "sender": {"user_id": 1}, "message": "/小剧场 出图 id"},
    ]
    messages = normalize_history(rows, "99", 120, 12000)
    assert [m.name for m in messages] == ["甲", "乙"]
    assert "secret" not in str(messages) and messages[0].text == "你好"


def test_pasted_text_never_guesses_identity():
    result = pasted_messages("甲：你好\n乙：坏了\n甲：稳住")
    assert [x.speaker for x in result] == ["u1", "u2", "u1"]
    assert all(not x.user_id for x in result)


def test_bounded_history_prefers_recent():
    rows = [{"time": i, "sender": {"user_id": 1}, "message": "x" * 100} for i in range(100)]
    assert len(normalize_history(rows, "99", 20, 300)) == 3
