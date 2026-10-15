import copy
import json

import pytest

from core.story import CandidateValidationError, Message, validate_candidates
from tools.replay_candidates import replay


def test_intervening_group_messages_do_not_discard_complete_story(story, messages):
    expanded = (
        [messages[0]]
        + [Message(f"interruption-{i}", "bystander", "旁观者", "其他话题") for i in range(50)]
        + messages[1:]
    )
    diagnostics = []
    result = validate_candidates(
        json.dumps({"candidates": [story]}), expanded, 3, diagnostics.append
    )
    assert len(result) == 1
    assert result[0]["source_ids"] == story["source_ids"]
    assert len(result[0]["panels"]) == 4
    assert any("超过建议的40条" in issue for issue in diagnostics)


def test_every_rejected_candidate_keeps_reason(story, messages, caplog):
    long_line = copy.deepcopy(story)
    long_line["panels"][2]["dialogue"][0]["text"] = "字" * 25
    missing_source = copy.deepcopy(story)
    missing_source["source_ids"] = ["unknown"]
    diagnostics = []
    with pytest.raises(CandidateValidationError) as failure:
        validate_candidates(
            json.dumps({"candidates": [long_line, missing_source]}), messages, 3, diagnostics.append
        )
    assert len(failure.value.issues) == 2
    assert "候选1" in diagnostics[0]
    assert "panels[2].dialogue[0].text" in diagnostics[0]
    assert "25字" in diagnostics[0] and "24字" in diagnostics[0]
    assert "不存在的素材消息" in diagnostics[1]
    assert "Candidate validation:" not in caplog.text  # The callback owns logging.
    assert "字" * 25 not in caplog.text


def test_partial_success_still_reports_rejected_item(story, messages):
    invalid = copy.deepcopy(story)
    invalid["cast"][0]["appearance"] = ""
    diagnostics = []
    result = validate_candidates(
        json.dumps({"candidates": [invalid, story]}), messages, 3, diagnostics.append
    )
    assert len(result) == 1
    assert "cast[0].appearance" in diagnostics[0]


@pytest.mark.parametrize("raw", ["private non-json text", None, '{"candidates": ['])
def test_parse_errors_are_logged_without_payload(raw, messages, caplog):
    with pytest.raises(CandidateValidationError) as failure:
        validate_candidates(raw, messages, 3)
    assert failure.value.issues
    assert "private non-json text" not in str(failure.value)
    assert "private non-json text" not in caplog.text


@pytest.mark.parametrize("valid", [True, False])
async def test_replay_keeps_diagnostics_without_network(tmp_path, story, messages, valid):
    from dataclasses import asdict

    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "source": {"messages": [asdict(m) for m in messages]},
                "plugin_config": {"workflow": {}},
            }
        ),
        encoding="utf-8",
    )
    if not valid:
        story["panels"] = []
    response = tmp_path / "response.txt"
    response.write_text(json.dumps({"candidates": [story]}), encoding="utf-8")
    output = tmp_path / "result"
    report = await replay(input_path, response, output)
    assert report["status"] == ("success" if valid else "validation_failed")
    assert report["model_calls"] == report["image_calls"] == report["group_messages_sent"] == 0
    assert json.loads((output / "report.json").read_text(encoding="utf-8")) == report
    if valid:
        assert (output / "preview.txt").is_file()
    else:
        assert "必须恰好四格" in report["issues"][0]
        assert not (output / "candidates.json").exists()
