"""Replay a saved text-model response locally. No network, bot or image calls."""

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.store import Store
from core.story import Message, TheaterError, candidate_card
from core.workflow import Workflow


class NoDrawing:
    config = {}

    def validate(self):
        raise AssertionError("Offline replay must never generate images")

    async def avatar(self, *args):
        raise AssertionError("Offline replay must never fetch avatars")

    async def generate(self, *args):
        raise AssertionError("Offline replay must never generate images")


async def replay(input_path: Path, response_path: Path, output: Path) -> dict:
    data = json.loads(input_path.read_text(encoding="utf-8"))
    messages = [Message(**item) for item in data["source"]["messages"]]
    raw = response_path.read_text(encoding="utf-8")
    output.mkdir(parents=True, exist_ok=True)
    store = Store(output)
    report = {
        "status": "pending",
        "source_count": len(messages),
        "issues": [],
        "model_calls": 0,
        "image_calls": 0,
        "group_messages_sent": 0,
    }

    async def recorded_response(prompt):
        return raw

    try:
        drafts = await Workflow(
            store,
            recorded_response,
            NoDrawing(),
            data["plugin_config"]["workflow"],
            on_diagnostic=report["issues"].append,
        ).candidates("local-internal-test", "local-review", messages)
        (output / "candidates.json").write_text(
            json.dumps(drafts, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        cards = "\n\n---\n\n".join(candidate_card(draft, i) for i, draft in enumerate(drafts, 1))
        (output / "preview.txt").write_text(cards, encoding="utf-8")
        report.update(status="success", candidate_count=len(drafts), ids=[d["id"] for d in drafts])
    except TheaterError as exc:
        report.update(status="validation_failed", error=str(exc))
    except Exception as exc:
        report.update(status="error", error_type=type(exc).__name__)
        raise
    finally:
        store.close()
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, required=True, help="Private redacted config and source JSON"
    )
    parser.add_argument("--response", type=Path, required=True, help="Saved model response text")
    parser.add_argument("--output", type=Path, help="Private output directory")
    args = parser.parse_args()
    output = args.output or args.input.parent / ("replay-" + uuid.uuid4().hex[:8])
    report = asyncio.run(replay(args.input, args.response, output))
    print(json.dumps({"output": str(output), **report}, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "success" else 1)
