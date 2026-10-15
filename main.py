"""AstrBot integration. All actions are explicit administrator commands."""

import asyncio

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Node, Nodes, Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.star.filter.command import GreedyStr

from .core.drawing import DrawingClient
from .core.source import fetch_history, pasted_messages
from .core.store import Store
from .core.story import SYSTEM_PROMPT, TheaterError, candidate_card
from .core.workflow import Workflow

HELP = """群友四格小剧场（管理员）
/小剧场 取材 [条数]：返回多个小剧场候选的合并转发，每个含ID和四格分镜
/小剧场 编剧 昵称：发言…：粘贴对话创作；每人发言换行，不自动推断QQ身份
/小剧场 分镜 <ID>：重新查看候选
/小剧场 出图 <ID>：明确确认，提交一次绘图；成功后重复命令仅重发缓存图片
/小剧场 状态 <ID>：查看结果状态
/小剧场 取消 <ID>：取消未出图草稿
/小剧场 额度：查看本插件滚动窗口提交数
/小剧场 清理：删除所有已过期素材和成图
无定时任务、无群消息监听、无自动出图、无绘图重试。
首次使用请在插件配置中启用并填写群白名单、编剧模型及绘图入口。
"""


@register("astrbot_plugin_group_theater", "mkroen", "群友四格小剧场", "0.2.0")
class GroupTheaterPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.store = Store(StarTools.get_data_dir("astrbot_plugin_group_theater"))
        self.busy = asyncio.Lock()
        self.tasks = set()
        self.closing = False

    async def terminate(self):
        self.closing = True
        pending = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.store.close()

    def _drawing(self):
        settings = dict(self.config.get("drawing", {}))
        key = str(settings.get("api_key") or "")
        source_id = settings.get("credential_source_id")
        if not key and source_id:
            sources = self.context.get_config().get("provider_sources", [])
            source = next((x for x in sources if x.get("id") == source_id), {})
            keys = source.get("key") or []
            key = keys[0] if isinstance(keys, list) and keys else ""
            if not settings.get("api_base"):
                settings["api_base"] = source.get("api_base", "")
        return DrawingClient(settings, key)

    async def _generate_text(self, event, prompt):
        provider_id = self.config.get("story_provider_id", "").strip()
        if not provider_id:
            provider = await self.context.get_using_provider_async(umo=event.unified_msg_origin)
            if not provider:
                raise TheaterError("未配置可用的编剧文字模型。")
            provider_id = provider.meta().id
        try:
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=SYSTEM_PROMPT,
                ),
                timeout=int(self.config.get("story_timeout", 120)),
            )
            return response.completion_text
        except Exception:
            raise TheaterError("编剧模型调用失败或超时，未调用绘图模型。") from None

    def _candidate_result(self, event, drafts):
        heading = f"小剧场候选 · {len(drafts)}个\n每个候选已备好四格分镜，选中后发送：\n/小剧场 出图 <ID>\n当前仅生成文字，尚未出图。"
        texts = [heading] + [candidate_card(draft, i) for i, draft in enumerate(drafts, 1)]
        nodes = [
            Node(uin=str(event.get_self_id()), name="小剧场候选", content=[Plain(text)])
            for text in texts
        ]
        return event.chain_result([Nodes(nodes)])

    @filter.command("小剧场")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def theater(self, event: AstrMessageEvent, arguments: GreedyStr):
        event.should_call_llm(False)
        event.stop_event()
        action, _, rest = str(arguments).strip().partition(" ")
        if action in {"", "帮助", "help"}:
            yield event.plain_result(HELP)
            return
        if self.closing:
            return
        group = str(event.get_group_id() or "")
        allowed = [str(x) for x in self.config.get("allowed_groups", [])]
        if not self.config.get("enabled", False) or not group or group not in allowed:
            yield event.plain_result("小剧场未启用或本群不在白名单，请在插件配置中设置。")
            return
        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("第一版仅支持 OneBot/NapCat 群聊，其他平台尚未适配。")
            return
        if self.busy.locked():
            yield event.plain_result("已有小剧场操作正在执行，请稍后再试。不会排队补发绘图。")
            return
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            async with self.busy:
                cleaned = self.store.cleanup()
                scope, owner = event.unified_msg_origin, str(event.get_sender_id())
                workflow = Workflow(
                    self.store,
                    lambda p: self._generate_text(event, p),
                    self._drawing(),
                    dict(self.config.get("workflow", {})),
                )
                did = rest.strip().split(" ", 1)[0]
                if action == "取材":
                    limit = max(10, min(200, int(self.config.get("history_limit", 120))))
                    count = int(did) if did.isdigit() else limit
                    if not 10 <= count <= limit:
                        raise TheaterError(f"取材条数应在10至{limit}之间。")
                    yield event.plain_result("正在取材并准备小剧场候选与四格分镜；选ID后才出图。")
                    messages = await asyncio.wait_for(
                        fetch_history(event, count, 12000), timeout=30
                    )
                    drafts = await workflow.candidates(scope, owner, messages)
                    yield self._candidate_result(event, drafts)
                elif action == "编剧":
                    # GreedyStr joins tokens; use the original body to preserve pasted newlines.
                    body = event.get_message_str().partition("编剧")[2].strip() or rest
                    messages = pasted_messages(body)
                    yield event.plain_result("正在编写文字分镜；粘贴素材不自动绑定群友头像。")
                    drafts = await workflow.candidates(scope, owner, messages)
                    yield self._candidate_result(event, drafts)
                elif action == "分镜":
                    yield event.plain_result(candidate_card(self.store.get(did, scope, owner)))
                elif action == "出图":
                    path = await workflow.render(did, scope, owner)
                    yield event.chain_result([Image.fromFileSystem(str(path))])
                    yield event.plain_result(
                        f"小剧场 {did} 已完成。再次确认将重发这张图，不会重新生成。"
                    )
                elif action == "状态":
                    draft = self.store.get(did, scope, owner)
                    yield event.plain_result(
                        f"{did} · 第{draft['revision']}稿 · {draft['status']}\n{draft['error']}"
                    )
                elif action == "取消":
                    self.store.cancel(self.store.get(did, scope, owner))
                    yield event.plain_result("已取消草稿，未调用绘图。")
                elif action == "额度":
                    s = workflow.settings
                    hours, limit = int(s.get("image_window_hours", 5)), int(s.get("image_limit", 1))
                    used = self.store.quota(hours * 3600)
                    yield event.plain_result(
                        f"本插件最近{hours}小时提交{used}/{limit}次。失败、超时也计数；其他插件和CPA客户端的用量不在此统计。"
                    )
                elif action == "清理":
                    yield event.plain_result(
                        f"已清理{cleaned}份过期素材及其成图。绘图额度记录保留。"
                    )
                else:
                    yield event.plain_result(HELP)
        except TheaterError as exc:
            yield event.plain_result(str(exc))
        except asyncio.TimeoutError:
            yield event.plain_result("读取素材超时，未提交绘图。")
        except Exception as exc:
            # Exception messages may contain URLs or provider credentials. Log type only.
            logger.error("小剧场操作失败：%s", type(exc).__name__)
            yield event.plain_result("小剧场操作失败，未自动重试。请查看任务状态。")
        finally:
            self.tasks.discard(task)
