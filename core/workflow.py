"""Explicit draft -> revision -> render flow; no image calls in drafting."""

import asyncio
from dataclasses import asdict

from .story import (
    Message,
    TheaterError,
    candidates_prompt,
    drawing_prompt,
    story_prompt,
    validate_candidates,
    validate_story,
)


class Workflow:
    def __init__(self, store, text_generate, drawing, settings: dict):
        self.store = store
        self.text_generate = text_generate
        self.drawing = drawing
        self.settings = dict(settings)
        for key, default, maximum in [
            ("retention_hours", 24, 720),
            ("candidate_count", 3, 5),
        ]:
            try:
                value = int(self.settings.get(key, default))
            except (TypeError, ValueError):
                raise TheaterError(f"配置 {key} 必须为整数。") from None
            if not 1 <= value <= maximum:
                raise TheaterError(f"配置 {key} 必须在1至{maximum}之间。")
            self.settings[key] = value

    async def candidates(self, scope: str, owner: str, messages: list[Message]) -> list[dict]:
        count = self.settings["candidate_count"]
        raw = await self.text_generate(candidates_prompt(messages, count))
        stories = validate_candidates(raw, messages, count)
        source = [asdict(m) for m in messages]
        ttl = self.settings["retention_hours"] * 3600
        # Persist the whole batch atomically, so partial writes cannot hide candidates.
        self.store.db.execute("BEGIN IMMEDIATE")
        try:
            drafts = [self.store.create(scope, owner, source, story, ttl) for story in stories]
            self.store.db.execute("COMMIT")
        except BaseException:
            self.store.db.execute("ROLLBACK")
            raise
        return drafts

    async def draft(self, scope: str, owner: str, messages: list[Message]) -> dict:
        raw = await self.text_generate(story_prompt(messages))
        story = validate_story(raw, messages)
        return self.store.create(
            scope,
            owner,
            [asdict(m) for m in messages],
            story,
            int(self.settings.get("retention_hours", 24)) * 3600,
        )

    async def revise(self, did: str, scope: str, owner: str, feedback: str) -> dict:
        draft = self.store.get(did, scope, owner)
        if draft["status"] != "draft":
            raise TheaterError("只有未提交绘图的草稿可以改稿。")
        if not feedback.strip() or len(feedback) > 2000:
            raise TheaterError("请填写1至2000字的修改意见。")
        messages = [Message(**x) for x in draft["source"]]
        raw = await self.text_generate(story_prompt(messages, draft["story"], feedback))
        return self.store.revise(draft, validate_story(raw, messages))

    async def render(self, did: str, scope: str, owner: str):
        draft = self.store.get(did, scope, owner)
        if draft["status"] == "done":
            path = self.store.root / "images" / (did + ".png")
            if path.is_file():
                return path  # resend cached result; never draw twice
            raise TheaterError("结果文件已丢失；不会自动重新出图。")
        if draft["status"] not in {"draft", "failed", "unknown"}:
            raise TheaterError(f"当前状态为{draft['status']}，不会重复提交。")
        self.drawing.validate()
        refs, ref_ids = [], []
        people = {m["speaker"]: m for m in draft["source"]}
        if self.settings.get("use_avatars", True):
            for character in draft["story"]["cast"]:
                uid = people[character["id"]]["user_id"]
                if not uid:
                    continue  # pasted dialogue has no verified QQ identity
                try:
                    refs.append(await self.drawing.avatar(uid))
                    ref_ids.append(character["id"])
                except Exception:
                    raise TheaterError(
                        f"角色 {character['name']} 的头像获取失败；未调用绘图。可修复网络或关闭头像后再试。"
                    ) from None
        if (
            refs
            and self.drawing.config.get("protocol") == "openai_images"
            and self.drawing.config.get("generations_only")
        ):
            raise TheaterError("当前入口仅支持文生图，不能携带头像；未提交绘图。")
        prompt = drawing_prompt(
            draft["story"],
            ref_ids,
            self.settings.get("style", "清晰的轻松日常漫画，简洁背景，生动表情"),
        )
        self.store.reserve(draft)
        try:
            image = await self.drawing.generate(prompt, refs)
            folder = self.store.root / "images"
            folder.mkdir(exist_ok=True)
            path = folder / (did + ".png")
            temporary = path.with_suffix(".png.tmp")
            temporary.write_bytes(image)
            temporary.replace(path)
            self.store.finish(did, "done", path.name)
            return path
        except asyncio.CancelledError:
            self.store.finish(did, "unknown", error="任务中断，结果未知，不会自动重发。")
            raise
        except Exception as exc:
            safe = (
                str(exc)
                if isinstance(exc, TheaterError)
                else "出图或保存失败，结果未知，不会重试。"
            )
            self.store.finish(did, "failed", error=safe)
            raise TheaterError(safe) from None
