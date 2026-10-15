"""On-demand OneBot history only; no all-message listener or passive analysis."""

import re

from .story import Message, TheaterError


def plain_text(content) -> str:
    if isinstance(content, str):
        return re.sub(r"\[CQ:[^\]]+\]", "", content).strip()
    if isinstance(content, list):
        parts = []
        for seg in content:
            if not isinstance(seg, dict):
                continue
            data = seg.get("data", {})
            if not isinstance(data, dict):
                continue
            if seg.get("type") == "text":
                parts.append(str(data.get("text", "")))
            elif seg.get("type") == "image":
                parts.append("[图片，未识别内容]")
        return "".join(parts).strip()
    return ""


def normalize_history(rows: list[dict], bot_id: str, limit: int, max_chars: int) -> list[Message]:
    if not isinstance(rows, list):
        raise TheaterError("群历史返回格式不正确。")

    # NapCat typically returns chronologically ordered pages. Stable timestamp sort
    # also accommodates reverse pages without inventing a new ordering for ties.
    def timestamp(row):
        try:
            return int(row.get("time", 0))
        except (TypeError, ValueError):
            return 0

    rows = sorted([r for r in rows if isinstance(r, dict)], key=timestamp)
    candidates = []
    seen = set()
    for row in rows:
        sender = row.get("sender") or {}
        if not isinstance(sender, dict):
            continue
        uid = str(sender.get("user_id") or row.get("user_id") or "")
        text = plain_text(row.get("message", row.get("raw_message", "")))
        mid = str(row.get("message_id", ""))
        if not uid or uid == str(bot_id) or not text or text.lstrip("/").startswith("小剧场"):
            continue
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        name = str(sender.get("card") or sender.get("nickname") or "群友")[:40]
        candidates.append((uid, name, text[:800], timestamp(row)))
    bounded = []
    used = 0
    for row in reversed(candidates[-limit:]):
        if used + len(row[2]) > max_chars:
            break
        used += len(row[2])
        bounded.append(row)
    people = {}
    result = []
    for uid, name, text, ts in reversed(bounded):
        speaker = people.setdefault(uid, f"u{len(people) + 1}")
        result.append(Message(f"m{len(result) + 1}", speaker, name, text, uid, ts))
    if not result:
        raise TheaterError("没有可用文字聊天；不会分析图片或自动扩大历史范围。")
    return result


async def fetch_history(event, count: int, max_chars: int) -> list[Message]:
    if event.get_platform_name() != "aiocqhttp" or not event.get_group_id():
        raise TheaterError("取材暂只支持 OneBot/NapCat 群聊。")
    try:
        result = await event.bot.api.call_action(
            "get_group_msg_history", group_id=int(event.get_group_id()), count=count
        )
    except Exception:
        raise TheaterError("获取本群历史失败；请确认协议端支持 get_group_msg_history。") from None
    if isinstance(result, dict) and isinstance(result.get("data"), dict):
        result = result["data"]
    rows = result.get("messages", []) if isinstance(result, dict) else []
    return normalize_history(rows, event.get_self_id(), count, max_chars)


def pasted_messages(text: str, max_chars: int = 12000) -> list[Message]:
    if not text.strip() or len(text) > max_chars:
        raise TheaterError(f"请提供对话素材，最多{max_chars}字。格式：每行“昵称：发言”。")
    people = {}
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^([^：:]{1,30})[：:]\s*(.+)$", line)
        name, content = match.groups() if match else ("叙述者", line)
        speaker = people.setdefault(name, f"u{len(people) + 1}")
        result.append(Message(f"m{len(result) + 1}", speaker, name, content[:800]))
    if not result:
        raise TheaterError("没有可用素材。")
    return result[:200]
