from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

try:
    from astrbot.api import AstrBotConfig, logger
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.provider import LLMResponse, ProviderRequest
    from astrbot.api.star import Context, Star, register
except ImportError:
    logger = logging.getLogger("astrbot_plugin_dify_memory")
    AstrBotConfig = dict  # type: ignore[misc,assignment]
    AstrMessageEvent = Any  # type: ignore[misc,assignment]
    ProviderRequest = Any  # type: ignore[misc,assignment]
    LLMResponse = Any  # type: ignore[misc,assignment]
    Context = Any  # type: ignore[misc,assignment]

    class Star:  # type: ignore[no-redef]
        def __init__(self, context: Any = None) -> None:
            self.context = context

    def register(name: str, author: str, desc: str, version: str, repo: str = "") -> Callable[[Any], Any]:  # type: ignore[misc]
        def _decorator(cls: Any) -> Any:
            return cls

        return _decorator

    class _CommandGroupStub:
        def __init__(self, func: Callable[..., Any]) -> None:
            self.func = func

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            return self.func(*args, **kwargs)

        def command(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            def _dec(fn: Callable[..., Any]) -> Callable[..., Any]:
                return fn

            return _dec

        def group(self, name: str) -> Callable[[Callable[..., Any]], "_CommandGroupStub"]:
            def _dec(fn: Callable[..., Any]) -> "_CommandGroupStub":
                return _CommandGroupStub(fn)

            return _dec

    class _FilterStub:
        @staticmethod
        def on_llm_request() -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            return lambda fn: fn

        @staticmethod
        def on_llm_response() -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            return lambda fn: fn

        @staticmethod
        def command(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            return lambda fn: fn

        @staticmethod
        def command_group(name: str) -> Callable[[Callable[..., Any]], _CommandGroupStub]:
            return lambda fn: _CommandGroupStub(fn)

    filter = _FilterStub()  # type: ignore[assignment]

try:
    from .dify_client import DifyAPIError, DifyDatasetClient, normalize_keywords
    from .storage import MemoryStorage
except ImportError:
    import sys

    _PLUGIN_DIR = str(Path(__file__).resolve().parent)
    if _PLUGIN_DIR not in sys.path:
        sys.path.insert(0, _PLUGIN_DIR)
    from dify_client import DifyAPIError, DifyDatasetClient, normalize_keywords  # type: ignore[no-redef]
    from storage import MemoryStorage  # type: ignore[no-redef]

SUMMARY_SYSTEM_PROMPT = (
    "你是一个专业的长期记忆提炼助手。你的任务是从用户与AI助手的多轮对话中，"
    "提炼出关于该【用户】的有价值的长期事实、个人偏好、身份背景、长期计划或重要经历。\n"
    "请严格遵守以下规则：\n"
    "1. 只提取具有长期保留价值的用户画像、事实或偏好，忽略日常寒暄、单纯玩梗、临时指令或无意义的闲聊。\n"
    "2. 每条记忆必须独立成句、语义完整、客观准确，以“用户...”或明确的事实陈述开头。\n"
    "3. 如果这批对话中没有任何值得长期记住的信息，请直接输出空数组 `[]`。\n"
    "4. 必须且只能输出合法的 JSON 数组，不要输出任何额外解释文字。格式示例：\n"
    '[\n  {"content": "用户是一名Python后端工程师，日常使用FastAPI框架", "keywords": ["Python", "后端工程师", "FastAPI"]}\n]'
)

CODE_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def parse_summary_json(raw_text: str) -> List[Dict[str, Any]]:
    """
    解析大模型输出的记忆 JSON 数组，兼容 Markdown 代码块包裹或对象包装格式。
    """
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("LLM 返回内容为空")

    fence_match = CODE_FENCE_PATTERN.search(text)
    if fence_match:
        text = fence_match.group(1).strip()

    parsed: Any = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # 使用 JSONDecoder.raw_decode 精确扫描文本中的第一个合法 JSON 数组或对象，
        # 避免说明文字中带有 `[` 或 `]`（例如“由于没有新记忆 [见对话]，输出如下：[]”）导致首尾截取错位。
        decoder = json.JSONDecoder()
        found = False
        for idx, ch in enumerate(text):
            if ch in ("[", "{"):
                try:
                    candidate, _ = decoder.raw_decode(text[idx:])
                    if isinstance(candidate, (list, dict)):
                        parsed = candidate
                        found = True
                        break
                except json.JSONDecodeError:
                    continue
        if not found:
            raise

    if isinstance(parsed, dict):
        for key in ("memories", "items", "data", "segments"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        else:
            if "content" in parsed:
                parsed = [parsed]
            else:
                raise ValueError(f"无法从 JSON 对象中识别记忆列表: {parsed}")

    if not isinstance(parsed, list):
        raise ValueError(f"期望 JSON 数组，实际得到: {type(parsed).__name__}")

    results: List[Dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, str):
            clean_str = item.strip()
            if clean_str:
                results.append({"content": clean_str, "keywords": []})
        elif isinstance(item, dict):
            content = str(item.get("content") or item.get("memory") or item.get("fact") or "").strip()
            if not content:
                continue
            keywords = normalize_keywords(item.get("keywords"))
            results.append({"content": content, "keywords": keywords})

    return results


def build_summary_user_prompt(user_id: str, turns: List[Dict[str, str]]) -> str:
    """将多轮对话组装为供大模型总结的提示词。"""
    lines = [
        f"以下是用户 (ID: {user_id}) 最近的 {len(turns)} 轮对话记录，请提炼该用户的长期记忆并输出 JSON 数组：",
        "",
    ]
    for idx, turn in enumerate(turns, start=1):
        ts = turn.get("timestamp") or ""
        u_msg = turn.get("user") or ""
        a_msg = turn.get("assistant") or ""
        prefix = f"[第{idx}轮 {ts}]" if ts else f"[第{idx}轮]"
        lines.append(f"{prefix}\n用户: {u_msg}\n助手: {a_msg}\n")
    return "\n".join(lines)


def format_memories_for_system_prompt(memories: List[Dict[str, Any]]) -> str:
    """将检索到的用户长期记忆格式化为注入 system_prompt 的文本块。"""
    if not memories:
        return ""
    lines = [
        "\n\n<user_long_term_memory>",
        "以下是从云端长期记忆库中检索到的关于当前对话用户的历史记忆，请在回复时自然参考，无需刻意复述记忆标签：",
    ]
    for idx, mem in enumerate(memories, start=1):
        clean_text = mem.get("clean_content") or mem.get("content") or ""
        time_str = mem.get("time") or ""
        if time_str:
            lines.append(f"{idx}. [{time_str}] {clean_text}")
        else:
            lines.append(f"{idx}. {clean_text}")
    lines.append("</user_long_term_memory>")
    return "\n".join(lines)


@register(
    "astrbot_plugin_dify_memory",
    "zaixiakongyiji",
    "基于 Dify 云端知识库的跨群用户级长期记忆插件",
    "1.0.1",
    "https://github.com/zaixiakongyiji/astrbot_plugin_dify_memory",
)
class DifyMemoryPlugin(Star):
    """AstrBot × Dify 云端长期记忆插件主类。"""

    def __init__(
        self,
        context: Context,
        config: Optional[AstrBotConfig] = None,
        data_dir: Optional[Path | str] = None,
    ) -> None:
        super().__init__(context)
        self.config: Dict[str, Any] = config if config is not None else {}
        self.storage = MemoryStorage(data_dir=data_dir)
        self._pending_prompts: Dict[str, str] = {}
        self._bg_tasks: Set[asyncio.Task[Any]] = set()

    def _build_dify_client(self) -> DifyDatasetClient:
        """根据当前配置构建 DifyDatasetClient。"""
        return DifyDatasetClient(
            base_url=str(self.config.get("dify_base_url") or "https://api.dify.ai/v1"),
            api_key=str(self.config.get("dify_dataset_api_key") or ""),
            dataset_id=str(self.config.get("dify_dataset_id") or ""),
            timeout=int(self.config.get("request_timeout") or 15),
        )

    @staticmethod
    def _get_user_id(event: AstrMessageEvent) -> str:
        """获取跨群唯一的平台用户 ID。"""
        if event is None:
            return ""
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            uid = getter()
            if uid is not None:
                return str(uid).strip()
        return ""

    @staticmethod
    def _get_event_key(event: AstrMessageEvent, user_id: str) -> str:
        """生成用于配对单次 LLM 请求与响应的键，兼顾同一用户在不同群并发对话。"""
        umo = getattr(event, "unified_msg_origin", None)
        if umo:
            return f"{user_id}::{umo}"
        return user_id

    @staticmethod
    def _extract_subcommand_arg(event: AstrMessageEvent, subcommand: str, fallback: str = "") -> str:
        """
        提取 `/mem add <内容>` 或 `/mem search <关键词>` 的完整剩余文本，
        避免包含空格的句子被命令分词器截断。
        """
        raw_msg = (getattr(event, "message_str", "") or "").strip()
        if raw_msg:
            pattern = re.compile(
                rf"^/?mem\s+{re.escape(subcommand)}\s+(.+)$",
                re.DOTALL | re.IGNORECASE,
            )
            match = pattern.match(raw_msg)
            if match:
                return match.group(1).strip()
        return (fallback or "").strip()

    async def _call_summary_llm(self, prompt: str, umo: Optional[str] = None) -> str:
        """
        调用 AstrBot 已配置的模型提供商执行对话总结：
        1. 若配置了 summary_provider_id，则优先使用指定提供商；
        2. 若留空，则自动获取当前会话正在使用的模型提供商；
        3. 兼容新版 context.llm_generate 与旧版 provider.text_chat 接口。
        """
        configured_provider_id = str(self.config.get("summary_provider_id") or "").strip()
        provider_id = configured_provider_id

        # 1. 尝试新版 AstrBot v4 context.llm_generate 接口
        if hasattr(self.context, "llm_generate"):
            if not provider_id and hasattr(self.context, "get_current_chat_provider_id"):
                try:
                    provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                except TypeError:
                    provider_id = await self.context.get_current_chat_provider_id()
                except Exception as exc:
                    logger.warning(f"[DifyMemory] 获取当前会话 provider_id 失败: {exc}")

            if provider_id:
                try:
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        system_prompt=SUMMARY_SYSTEM_PROMPT,
                    )
                except TypeError:
                    combined_prompt = f"{SUMMARY_SYSTEM_PROMPT}\n\n{prompt}"
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=combined_prompt,
                    )
                text = getattr(llm_resp, "completion_text", "") if llm_resp else ""
                if text:
                    return str(text).strip()

        # 2. 回退使用 get_provider_by_id / get_using_provider + provider.text_chat
        provider = None
        if configured_provider_id and hasattr(self.context, "get_provider_by_id"):
            try:
                provider = self.context.get_provider_by_id(configured_provider_id)
            except Exception as exc:
                logger.warning(
                    f"[DifyMemory] 通过 summary_provider_id ({configured_provider_id}) 获取模型失败: {exc}"
                )

        if provider is None and hasattr(self.context, "get_using_provider"):
            try:
                provider = self.context.get_using_provider(umo=umo)
            except TypeError:
                provider = self.context.get_using_provider()

        if provider is None:
            raise RuntimeError("未能获取到可用的 AstrBot 模型提供商，请检查模型配置")

        try:
            llm_resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=SUMMARY_SYSTEM_PROMPT,
            )
        except TypeError:
            llm_resp = await provider.text_chat(f"{SUMMARY_SYSTEM_PROMPT}\n\n{prompt}")

        completion = getattr(llm_resp, "completion_text", "") if llm_resp else ""
        if not completion and hasattr(llm_resp, "result_chain") and llm_resp.result_chain:
            get_plain = getattr(llm_resp.result_chain, "get_plain_text", None)
            if callable(get_plain):
                completion = get_plain()
        return str(completion or "").strip()

    async def _summarize_and_store(
        self,
        user_id: str,
        turns: List[Dict[str, str]],
        umo: Optional[str] = None,
    ) -> Tuple[int, str]:
        """
        后台调用大模型提炼 N 轮对话中的长期记忆并写入 Dify 知识库。
        若网络或接口调用异常，自动将未归档的对话轮次恢复回缓冲区，避免丢失。
        返回 (成功写入的分段数, document_id)。
        """
        if not turns:
            return (0, self.storage.get_document_id(user_id) or "")

        client = self._build_dify_client()
        if not client.is_configured:
            logger.warning("[DifyMemory] Dify 知识库未配置，已跳过自动总结并将对话保留在缓冲区。")
            summary_turns = max(1, int(self.config.get("summary_turns") or 10))
            await self.storage.restore_turns(user_id, turns, max_keep=summary_turns * 3)
            return (0, "")

        user_prompt = build_summary_user_prompt(user_id, turns)
        try:
            raw_summary = await self._call_summary_llm(user_prompt, umo=umo)
            memories = parse_summary_json(raw_summary)
        except Exception as exc:
            logger.error(f"[DifyMemory] 用户 {user_id} 的对话总结生成或解析失败: {exc}")
            summary_turns = max(1, int(self.config.get("summary_turns") or 10))
            await self.storage.restore_turns(user_id, turns, max_keep=summary_turns * 3)
            raise

        if not memories:
            logger.info(
                f"[DifyMemory] 用户 {user_id} 本批次 ({len(turns)} 轮) 无新增长期记忆需归档。"
            )
            return (0, self.storage.get_document_id(user_id) or "")

        user_lock = self.storage.get_user_lock(user_id)
        try:
            async with user_lock:
                current_doc_id = self.storage.get_document_id(user_id)
                new_doc_id, written_count = await client.upsert_user_memories(
                    user_id=user_id,
                    current_doc_id=current_doc_id,
                    memories=memories,
                )
                if new_doc_id and new_doc_id != current_doc_id:
                    await self.storage.set_document_id(user_id, new_doc_id)
                logger.info(
                    f"[DifyMemory] 已为用户 {user_id} 归档 {written_count} 条长期记忆 (Document ID: {new_doc_id})。"
                )
                return (written_count, new_doc_id)
        except Exception as exc:
            logger.error(f"[DifyMemory] 写入用户 {user_id} 的 Dify 知识库失败: {exc}")
            summary_turns = max(1, int(self.config.get("summary_turns") or 10))
            await self.storage.restore_turns(user_id, turns, max_keep=summary_turns * 3)
            raise

    def _spawn_background_summary(
        self,
        user_id: str,
        turns: List[Dict[str, str]],
        umo: Optional[str] = None,
    ) -> asyncio.Task[Any]:
        """启动后台异步总结任务并维护引用集合。"""

        async def _runner() -> None:
            try:
                await self._summarize_and_store(user_id=user_id, turns=turns, umo=umo)
            except Exception as exc:
                logger.debug(f"[DifyMemory] 后台归档任务异常结束 (user={user_id}): {exc}")

        task = asyncio.create_task(_runner())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """
        在请求大模型前：
        1. 记录当前用户的输入文本（供响应阶段配对缓冲）；
        2. 从 Dify 知识库检索该用户的跨群长期记忆并注入 req.system_prompt。
        """
        user_id = self._get_user_id(event)
        if not user_id or req is None:
            return

        prompt_text = (
            getattr(req, "prompt", None)
            or getattr(event, "message_str", None)
            or ""
        ).strip()
        if not prompt_text:
            return

        # 忽略插件自身的 /mem 管理指令，防止管理操作污染对话缓冲
        if re.match(r"^/?mem(?:\s+|$)", prompt_text, re.IGNORECASE):
            return

        # 暂存本轮用户输入，限制字典上限防止异常中断请求导致内存堆积
        if len(self._pending_prompts) >= 500:
            oldest_key = next(iter(self._pending_prompts))
            self._pending_prompts.pop(oldest_key, None)
        event_key = self._get_event_key(event, user_id)
        self._pending_prompts[event_key] = prompt_text

        raw_min_len = self.config.get("min_query_length")
        min_query_len = max(1, int(raw_min_len if raw_min_len is not None else 2))
        if len(prompt_text) < min_query_len:
            return

        doc_id = self.storage.get_document_id(user_id)
        if not doc_id:
            return

        client = self._build_dify_client()
        if not client.is_configured:
            return

        raw_top_k = self.config.get("retrieve_top_k")
        top_k = int(raw_top_k if raw_top_k is not None else 5)
        raw_threshold = self.config.get("score_threshold")
        score_threshold = float(raw_threshold if raw_threshold is not None else 0.45)
        search_method = str(self.config.get("search_method") or "dataset_default")

        try:
            memories = await client.retrieve_memories(
                query=prompt_text,
                user_id=user_id,
                document_id=doc_id,
                top_k=top_k,
                score_threshold=score_threshold,
                search_method=search_method,
            )
            if memories:
                mem_block = format_memories_for_system_prompt(memories)
                current_sys = getattr(req, "system_prompt", "") or ""
                req.system_prompt = f"{current_sys}{mem_block}"
        except Exception as exc:
            logger.warning(f"[DifyMemory] 检索用户 {user_id} 记忆失败，将跳过本次注入: {exc}")

    @filter.on_llm_response()
    async def on_llm_response(
        self, event: AstrMessageEvent, resp: LLMResponse
    ) -> None:
        """
        在大模型生成回复后：
        1. 将本轮用户输入与助手回复配对写入该用户的对话缓冲区；
        2. 若累计达到 summary_turns 轮，弹出缓冲并在后台异步总结归档至 Dify。
        """
        user_id = self._get_user_id(event)
        if not user_id or resp is None:
            return

        event_key = self._get_event_key(event, user_id)
        user_prompt = self._pending_prompts.pop(event_key, None)
        if not user_prompt:
            user_prompt = (getattr(event, "message_str", "") or "").strip()

        if not user_prompt or re.match(r"^/?mem(?:\s+|$)", user_prompt, re.IGNORECASE):
            return

        reply_text = (getattr(resp, "completion_text", "") or "").strip()
        if not reply_text:
            return

        summary_turns = max(1, int(self.config.get("summary_turns") or 10))
        await self.storage.append_turn(
            user_id=user_id,
            user_msg=user_prompt,
            assistant_msg=reply_text,
        )

        turns_to_archive = await self.storage.pop_turns_if_ready(
            user_id=user_id,
            threshold=summary_turns,
        )
        if turns_to_archive:
            umo = getattr(event, "unified_msg_origin", None)
            self._spawn_background_summary(
                user_id=user_id,
                turns=turns_to_archive,
                umo=umo,
            )

    @filter.command_group("mem")
    def mem(self, event: AstrMessageEvent) -> None:
        """Dify 云端长期记忆管理指令组（status / force / add / search / clear）"""
        pass

    @mem.command("status")
    async def mem_status(self, event: AstrMessageEvent):
        """查看当前用户的 Dify 记忆文档绑定状态与未归档对话轮数。"""
        user_id = self._get_user_id(event)
        doc_id = self.storage.get_document_id(user_id) or "未创建（首次归档时自动生成）"
        buf_count = self.storage.get_buffer_count(user_id)
        summary_turns = max(1, int(self.config.get("summary_turns") or 10))
        client = self._build_dify_client()
        cfg_state = "已就绪" if client.is_configured else "未完成配置（请检查 API Key 与 Dataset ID）"

        msg = (
            f"📊 【Dify 长期记忆状态】\n"
            f"- 用户 ID：{user_id}\n"
            f"- 绑定文档 ID：{doc_id}\n"
            f"- 待归档对话缓冲：{buf_count} / {summary_turns} 轮\n"
            f"- Dify 连接配置：{cfg_state}\n"
            f"- 后台运行中归档任务数：{len(self._bg_tasks)}"
        )
        yield event.plain_result(msg)

    @mem.command("force")
    async def mem_force(self, event: AstrMessageEvent):
        """立即将当前用户缓冲区的对话强制总结并归档到 Dify。"""
        user_id = self._get_user_id(event)
        turns = await self.storage.pop_all_turns(user_id)
        if not turns:
            yield event.plain_result("当前没有待归档的对话缓冲记录。")
            return

        umo = getattr(event, "unified_msg_origin", None)
        try:
            written_count, doc_id = await self._summarize_and_store(
                user_id=user_id,
                turns=turns,
                umo=umo,
            )
            if written_count > 0:
                yield event.plain_result(
                    f"✅ 已强制总结 {len(turns)} 轮对话，成功向 Dify 归档 {written_count} 条长期记忆（Document ID: {doc_id}）。"
                )
            else:
                yield event.plain_result(
                    f"ℹ️ 已分析最近 {len(turns)} 轮对话，未发现需要长期归档的新事实或偏好。"
                )
        except Exception as exc:
            yield event.plain_result(
                f"❌ 强制总结归档失败（对话已恢复至本地缓冲区）：{exc}"
            )

    @mem.command("add")
    async def mem_add(self, event: AstrMessageEvent, content: str = ""):
        """手动添加一条关于自己的长期记忆。用法：/mem add <记忆内容>"""
        user_id = self._get_user_id(event)
        full_content = self._extract_subcommand_arg(event, "add", fallback=content)
        if not full_content:
            yield event.plain_result("请在指令后附上要记住的内容，例如：/mem add 我喜欢喝冰美式咖啡")
            return

        client = self._build_dify_client()
        if not client.is_configured:
            yield event.plain_result("❌ Dify 知识库未完成配置，请先在插件配置中填写 API Key 与 Dataset ID。")
            return

        user_lock = self.storage.get_user_lock(user_id)
        try:
            async with user_lock:
                current_doc_id = self.storage.get_document_id(user_id)
                new_doc_id, written_count = await client.upsert_user_memories(
                    user_id=user_id,
                    current_doc_id=current_doc_id,
                    memories=[{"content": full_content, "keywords": []}],
                )
                if new_doc_id and new_doc_id != current_doc_id:
                    await self.storage.set_document_id(user_id, new_doc_id)
            yield event.plain_result(
                f"✅ 已将该条记忆写入 Dify 云端知识库（Document ID: {new_doc_id}）。"
            )
        except DifyAPIError as exc:
            yield event.plain_result(f"❌ 写入 Dify 知识库失败：{exc}")
        except Exception as exc:
            yield event.plain_result(f"❌ 添加记忆时发生异常：{exc}")

    @mem.command("search")
    async def mem_search(self, event: AstrMessageEvent, query: str = ""):
        """从 Dify 检索当前用户的长期记忆。用法：/mem search <关键词>"""
        user_id = self._get_user_id(event)
        full_query = self._extract_subcommand_arg(event, "search", fallback=query)
        if not full_query:
            yield event.plain_result("请提供检索关键词，例如：/mem search 咖啡")
            return

        doc_id = self.storage.get_document_id(user_id)
        if not doc_id:
            yield event.plain_result("当前尚未创建你的云端记忆文档，暂无可检索的记忆。")
            return

        client = self._build_dify_client()
        if not client.is_configured:
            yield event.plain_result("❌ Dify 知识库未完成配置。")
            return

        raw_top_k = self.config.get("retrieve_top_k")
        top_k = int(raw_top_k if raw_top_k is not None else 5)
        raw_threshold = self.config.get("score_threshold")
        score_threshold = float(raw_threshold if raw_threshold is not None else 0.45)
        search_method = str(self.config.get("search_method") or "dataset_default")

        try:
            records = await client.retrieve_memories(
                query=full_query,
                user_id=user_id,
                document_id=doc_id,
                top_k=top_k,
                score_threshold=score_threshold,
                search_method=search_method,
            )
            if not records:
                yield event.plain_result(f"🔍 未检索到与「{full_query}」相关的长期记忆。")
                return

            lines = [f"🔍 检索到 {len(records)} 条与「{full_query}」相关的长期记忆："]
            for idx, rec in enumerate(records, start=1):
                score = rec.get("score")
                score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "-"
                time_str = rec.get("time") or "未知日期"
                lines.append(f"{idx}. [{time_str}] (相关度: {score_str}) {rec.get('clean_content')}")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"❌ 检索 Dify 记忆失败：{exc}")

    @mem.command("clear")
    async def mem_clear(self, event: AstrMessageEvent):
        """清空当前用户在 Dify 上的记忆文档及本地未归档缓冲。"""
        user_id = self._get_user_id(event)
        user_lock = self.storage.get_user_lock(user_id)
        async with user_lock:
            old_doc_id = self.storage.get_document_id(user_id)
            if old_doc_id:
                client = self._build_dify_client()
                if client.is_configured:
                    try:
                        await client.delete_user_document(old_doc_id)
                    except Exception as exc:
                        # 远端删除失败时保留本地 old_doc_id 映射，避免远端残留无法再被清理的孤儿文档，仅清空本地未归档缓冲
                        await self.storage.pop_all_turns(user_id)
                        yield event.plain_result(
                            f"⚠️ 本地未归档缓冲已清空，但删除远端 Dify 文档 ({old_doc_id}) 失败（已保留文档映射以便稍后重试 `/mem clear`）：{exc}"
                        )
                        return
            await self.storage.clear_user_state(user_id)
        yield event.plain_result("🗑️ 已清空你的本地未归档对话缓冲与 Dify 云端记忆文档。")
