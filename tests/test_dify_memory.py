from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from astrbot_plugin_dify_memory.dify_client import (
    DifyAPIError,
    DifyDatasetClient,
    DifyDocumentNotFoundError,
    format_memory_content,
    strip_memory_prefix,
)
from astrbot_plugin_dify_memory.main import (
    DifyMemoryPlugin,
    format_memories_for_system_prompt,
    parse_summary_json,
)
from astrbot_plugin_dify_memory.storage import MemoryStorage


class FakeHTTPResponse:
    def __init__(self, status: int, body: Dict[str, Any] | str) -> None:
        self.status = status
        self._body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> "FakeHTTPResponse":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class FakeClientSession:
    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls: List[Dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: Any = None,
    ) -> FakeHTTPResponse:
        call_info = {
            "method": method,
            "url": url,
            "headers": headers,
            "json": json,
            "params": params,
        }
        self.calls.append(call_info)
        status, body = self.handler(call_info)
        return FakeHTTPResponse(status, body)


class FakeEvent:
    def __init__(
        self,
        sender_id: str,
        message_str: str = "",
        umo: str = "qq:group:10001",
    ) -> None:
        self._sender_id = sender_id
        self.message_str = message_str
        self.unified_msg_origin = umo

    def get_sender_id(self) -> str:
        return self._sender_id

    def plain_result(self, text: str) -> str:
        return text


class FakeProviderRequest:
    def __init__(self, prompt: str, system_prompt: str = "你是一个贴心的AI助手。") -> None:
        self.prompt = prompt
        self.system_prompt = system_prompt


class FakeLLMResponse:
    def __init__(self, completion_text: str) -> None:
        self.completion_text = completion_text


class FakeContextV4:
    """模拟支持 get_current_chat_provider_id 与 llm_generate 的 AstrBot v4 Context。"""

    def __init__(self, summary_reply: str) -> None:
        self.summary_reply = summary_reply
        self.calls: List[Dict[str, Any]] = []

    async def get_current_chat_provider_id(self, umo: Optional[str] = None) -> str:
        return "default_v4_provider"

    async def llm_generate(
        self,
        chat_provider_id: str,
        prompt: str,
        system_prompt: Optional[str] = None,
    ) -> FakeLLMResponse:
        self.calls.append(
            {
                "chat_provider_id": chat_provider_id,
                "prompt": prompt,
                "system_prompt": system_prompt,
            }
        )
        return FakeLLMResponse(self.summary_reply)


class FakeLegacyProvider:
    def __init__(self, reply_text: str) -> None:
        self.reply_text = reply_text
        self.calls: List[Dict[str, Any]] = []

    async def text_chat(self, prompt: str, system_prompt: str = "") -> FakeLLMResponse:
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt})
        return FakeLLMResponse(self.reply_text)


class FakeContextLegacy:
    """模拟仅支持 get_using_provider().text_chat 的旧版 AstrBot Context。"""

    def __init__(self, reply_text: str) -> None:
        self.provider = FakeLegacyProvider(reply_text)

    def get_using_provider(self, umo: Optional[str] = None) -> FakeLegacyProvider:
        return self.provider


class TestDifyMemoryPlugin(unittest.IsolatedAsyncioTestCase):
    def test_parse_summary_json_variants(self) -> None:
        # 1. 标准 JSON 数组
        res1 = parse_summary_json(
            '[{"content": "用户住在上海", "keywords": ["上海", "居住地"]}]'
        )
        self.assertEqual(len(res1), 1)
        self.assertEqual(res1[0]["content"], "用户住在上海")
        self.assertEqual(res1[0]["keywords"], ["上海", "居住地"])

        # 2. Markdown 代码块包裹 + 前后解释文字
        fenced = (
            "好的，为您提炼如下：\n```json\n"
            '[\n  {"content": "用户养了一只名叫团子的布偶猫", "keywords": "布偶猫, 团子"}\n]\n'
            "```\n以上是结果。"
        )
        res2 = parse_summary_json(fenced)
        self.assertEqual(len(res2), 1)
        self.assertEqual(res2[0]["content"], "用户养了一只名叫团子的布偶猫")
        self.assertEqual(res2[0]["keywords"], ["布偶猫", "团子"])

        # 3. 对象包装 {"memories": [...]} 与纯字符串元素
        res3 = parse_summary_json('{"memories": ["用户喜欢喝乌龙茶"]}')
        self.assertEqual(res3, [{"content": "用户喜欢喝乌龙茶", "keywords": []}])

        # 4. 空数组（无有效长期记忆）
        self.assertEqual(parse_summary_json("```json\n[]\n```"), [])

        # 5. 前言说明文字包含 [中括号] 时仍能精确解析后续 JSON 数组
        bracket_noise = '分析说明 [第1轮-第10轮]：发现1条记忆，输出如下：\n[{"content": "用户喜欢吃拉面", "keywords": ["拉面"]}]'
        res_bracket = parse_summary_json(bracket_noise)
        self.assertEqual(len(res_bracket), 1)
        self.assertEqual(res_bracket[0]["content"], "用户喜欢吃拉面")

        # 6. 非法或空内容抛出异常
        with self.assertRaises(ValueError):
            parse_summary_json("")
        with self.assertRaises(Exception):
            parse_summary_json("这不是JSON")

    async def test_storage_persistence_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = MemoryStorage(data_dir=tmp)
            uid = "12345678"

            self.assertIsNone(storage.get_document_id(uid))
            await storage.set_document_id(uid, "doc-abc")
            self.assertEqual(storage.get_document_id(uid), "doc-abc")

            await storage.append_turn(uid, "你好", "你好呀", timestamp="2026-10-30 10:00:00")
            await storage.append_turn(uid, "我喜欢吃火锅", "记住了！", timestamp="2026-10-30 10:01:00")
            self.assertEqual(storage.get_buffer_count(uid), 2)

            # 阈值未达到时不弹出
            not_ready = await storage.pop_turns_if_ready(uid, threshold=3)
            self.assertEqual(not_ready, [])
            self.assertEqual(storage.get_buffer_count(uid), 2)

            # 重新加载磁盘验证持久化
            reloaded = MemoryStorage(data_dir=tmp)
            self.assertEqual(reloaded.get_document_id(uid), "doc-abc")
            self.assertEqual(reloaded.get_buffer_count(uid), 2)

            # 阈值达到时弹出
            popped = await reloaded.pop_turns_if_ready(uid, threshold=2)
            self.assertEqual(len(popped), 2)
            self.assertEqual(reloaded.get_buffer_count(uid), 0)

            # 失败恢复并测试最大保留上限
            await reloaded.append_turn(uid, "新对话", "新回复")
            await reloaded.restore_turns(uid, popped, max_keep=2)
            self.assertEqual(reloaded.get_buffer_count(uid), 2)
            final_turns = await reloaded.pop_all_turns(uid)
            self.assertEqual(final_turns[-1]["user"], "新对话")

    async def test_dify_client_create_append_404_recreate_and_isolation(self) -> None:
        state = {"doc_exists": True, "created_count": 0}

        def handler(call: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
            url = call["url"]
            method = call["method"]
            if method == "POST" and url.endswith("/document/create-by-text"):
                state["created_count"] += 1
                return (
                    200,
                    {"document": {"id": f"doc-created-{state['created_count']}"}},
                )
            if method == "POST" and "/segments" in url:
                if not state["doc_exists"]:
                    state["doc_exists"] = True
                    return (
                        404,
                        {"code": "document_not_found", "message": "Document not found."},
                    )
                return (200, {"data": [{"id": "seg-1"}]})
            if method == "POST" and url.endswith("/retrieve"):
                return (
                    200,
                    {
                        "records": [
                            {
                                "score": 0.88,
                                "segment": {
                                    "id": "seg-u1",
                                    "document_id": "doc-created-2",
                                    "content": "[USER:user_A] [TIME:2026-10-30] 用户A是一名架构师 [KEYWORDS:架构师]",
                                    "keywords": ["架构师"],
                                },
                            },
                            {
                                "score": 0.95,
                                "segment": {
                                    "id": "seg-u1-stale-doc",
                                    "document_id": "doc-created-1-stale",
                                    "content": "[USER:user_A] [TIME:2026-10-29] 用户A已废弃旧文档中的脏记忆",
                                    "keywords": [],
                                },
                            },
                            {
                                "score": 0.92,
                                "segment": {
                                    "id": "seg-u2",
                                    "document_id": "doc-other",
                                    "content": "[USER:user_B] [TIME:2026-10-30] 用户B喜欢滑雪",
                                    "keywords": ["滑雪"],
                                },
                            },
                            {
                                "score": 0.30,
                                "segment": {
                                    "id": "seg-u1-low",
                                    "document_id": "doc-created-2",
                                    "content": "[USER:user_A] [TIME:2026-10-30] 低分无关记录",
                                    "keywords": [],
                                },
                            },
                        ]
                    },
                )
            return (200, {})

        fake_session = FakeClientSession(handler)
        client = DifyDatasetClient(
            base_url="https://api.dify.ai/v1/",
            api_key="dataset-secret",
            dataset_id="ds-001",
            session=fake_session,  # type: ignore[arg-type]
        )

        # 1. 首次无 document_id：通过 SEGMENT_SEPARATOR 在单次 create-by-text 请求中同时写入 2 条分段（规避异步索引期调用 /segments 报 400）
        doc_id, count = await client.upsert_user_memories(
            user_id="user_A",
            current_doc_id=None,
            memories=[
                {"content": "用户A住在北京", "keywords": ["北京"]},
                {"content": "用户A是一名架构师", "keywords": ["架构师"]},
            ],
            date_str="2026-10-30",
        )
        self.assertEqual(doc_id, "doc-created-1")
        self.assertEqual(count, 2)
        self.assertEqual(len(fake_session.calls), 1)
        self.assertTrue(fake_session.calls[0]["url"].endswith("/document/create-by-text"))
        self.assertIn("===MEM_SEG===", fake_session.calls[0]["json"]["text"])
        self.assertIn("用户A住在北京", fake_session.calls[0]["json"]["text"])
        self.assertIn("用户A是一名架构师", fake_session.calls[0]["json"]["text"])

        # 2. 模拟远端文档被删（返回 404），触发自动重建
        state["doc_exists"] = False
        new_doc_id, count2 = await client.upsert_user_memories(
            user_id="user_A",
            current_doc_id=doc_id,
            memories=[{"content": "用户A喜欢喝手冲咖啡", "keywords": ["咖啡"]}],
            date_str="2026-10-30",
        )
        self.assertEqual(new_doc_id, "doc-created-2")
        self.assertEqual(count2, 1)

        # 3. 检索记忆：验证超长 query 截断（250字符）、低分过滤、跨用户隔离（过滤 user_B）与旧孤儿文档隔离（过滤 doc-created-1-stale）及 [KEYWORDS:...] 剥离
        long_query = "架构师" * 100
        matched = await client.retrieve_memories(
            query=long_query,
            user_id="user_A",
            document_id=new_doc_id,
            top_k=5,
            score_threshold=0.45,
        )
        retrieve_call = fake_session.calls[-1]
        self.assertEqual(len(retrieve_call["json"]["query"]), 250)
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["clean_content"], "用户A是一名架构师")
        self.assertEqual(matched[0]["time"], "2026-10-30")

    async def test_plugin_cross_group_turns_and_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary_json = json.dumps(
                [
                    {
                        "content": "用户是一名全栈开发者，正在给AstrBot编写Dify记忆插件",
                        "keywords": ["全栈开发者", "AstrBot", "Dify"],
                    }
                ],
                ensure_ascii=False,
            )
            ctx = FakeContextV4(summary_reply=f"```json\n{summary_json}\n```")
            config = {
                "dify_base_url": "https://api.dify.ai/v1",
                "dify_dataset_api_key": "dataset-key-123",
                "dify_dataset_id": "ds-uuid-999",
                "summary_provider_id": "",
                "summary_turns": 2,
                "retrieve_top_k": 5,
                "score_threshold": 0.45,
                "search_method": "hybrid_search",
                "min_query_length": 2,
            }
            plugin = DifyMemoryPlugin(context=ctx, config=config, data_dir=tmp)

            stored_segments: List[Dict[str, Any]] = []

            def dify_handler(call: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
                url = call["url"]
                if url.endswith("/document/create-by-text"):
                    stored_segments.append(
                        {"content": call["json"]["text"], "keywords": ["AstrBot"]}
                    )
                    return (200, {"document": {"id": "doc-qq-888"}})
                if "/segments" in url and call["method"] == "POST":
                    for seg in call["json"]["segments"]:
                        stored_segments.append(seg)
                    return (200, {"data": stored_segments})
                if url.endswith("/retrieve"):
                    return (
                        200,
                        {
                            "records": [
                                {
                                    "score": 0.91,
                                    "segment": {
                                        "id": f"seg-{idx}",
                                        "document_id": "doc-qq-888",
                                        "content": seg["content"],
                                        "keywords": seg.get("keywords", []),
                                    },
                                }
                                for idx, seg in enumerate(stored_segments)
                            ]
                        },
                    )
                if call["method"] == "DELETE":
                    stored_segments.clear()
                    return (200, {"result": "success"})
                return (200, {})

            fake_session = FakeClientSession(dify_handler)
            plugin._build_dify_client = lambda: DifyDatasetClient(  # type: ignore[method-assign]
                base_url=config["dify_base_url"],
                api_key=config["dify_dataset_api_key"],
                dataset_id=config["dify_dataset_id"],
                session=fake_session,  # type: ignore[arg-type]
            )

            # 第 1 轮对话（在群聊 10001）
            ev_group1 = FakeEvent(sender_id="888888", message_str="我在开发AstrBot插件", umo="qq:group:10001")
            req1 = FakeProviderRequest(prompt="我在开发AstrBot插件")
            await plugin.on_llm_request(ev_group1, req1)
            await plugin.on_llm_response(ev_group1, FakeLLMResponse("太棒了，需要什么帮助吗？"))
            self.assertEqual(plugin.storage.get_buffer_count("888888"), 1)

            # 第 2 轮对话（同一用户在另一个群聊 20002，跨群累计达到 summary_turns=2）
            ev_group2 = FakeEvent(sender_id="888888", message_str="我想用Dify知识库做长期记忆", umo="qq:group:20002")
            req2 = FakeProviderRequest(prompt="我想用Dify知识库做长期记忆")
            await plugin.on_llm_request(ev_group2, req2)
            await plugin.on_llm_response(ev_group2, FakeLLMResponse("用Dify的Dataset接口非常适合！"))

            # 等待后台总结协程执行完毕
            if plugin._bg_tasks:
                await asyncio.gather(*list(plugin._bg_tasks))

            self.assertEqual(plugin.storage.get_buffer_count("888888"), 0)
            self.assertEqual(plugin.storage.get_document_id("888888"), "doc-qq-888")
            self.assertEqual(len(stored_segments), 1)
            self.assertIn("[USER:888888]", stored_segments[0]["content"])

            # 第 3 轮对话（在私聊中提问，验证跨群记忆自动检索并注入 req.system_prompt）
            ev_private = FakeEvent(sender_id="888888", message_str="你还记得我在做什么项目吗？", umo="qq:private:888888")
            req3 = FakeProviderRequest(prompt="你还记得我在做什么项目吗？", system_prompt="基础人设。")
            await plugin.on_llm_request(ev_private, req3)
            self.assertIn("<user_long_term_memory>", req3.system_prompt)
            self.assertIn("正在给AstrBot编写Dify记忆插件", req3.system_prompt)

            # 测试手动指令 /mem add（带空格句子）
            ev_add = FakeEvent(sender_id="888888", message_str="/mem add 我喜欢喝冰美式咖啡 不加糖")
            add_outputs = [out async for out in plugin.mem_add(ev_add, "我喜欢喝冰美式咖啡")]
            self.assertTrue(any("✅" in o for o in add_outputs))
            self.assertEqual(len(stored_segments), 2)
            self.assertIn("我喜欢喝冰美式咖啡 不加糖", stored_segments[1]["content"])

            # 测试手动指令 /mem search
            ev_search = FakeEvent(sender_id="888888", message_str="/mem search 冰美式咖啡")
            search_outputs = [out async for out in plugin.mem_search(ev_search, "冰美式咖啡")]
            self.assertTrue(any("检索到 2 条" in o for o in search_outputs))

            # 测试手动指令 /mem status
            status_outputs = [out async for out in plugin.mem_status(ev_private)]
            self.assertTrue(any("doc-qq-888" in o for o in status_outputs))

            # 测试手动指令 /mem clear
            clear_outputs = [out async for out in plugin.mem_clear(ev_private)]
            self.assertTrue(any("🗑️" in o for o in clear_outputs))
            self.assertIsNone(plugin.storage.get_document_id("888888"))

    async def test_legacy_provider_fallback_and_failure_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy_ctx = FakeContextLegacy(
                reply_text='[{"content": "用户喜欢弹吉他", "keywords": ["吉他"]}]'
            )
            config = {
                "dify_base_url": "https://api.dify.ai/v1",
                "dify_dataset_api_key": "dataset-key",
                "dify_dataset_id": "ds-id",
                "summary_turns": 2,
            }
            plugin = DifyMemoryPlugin(context=legacy_ctx, config=config, data_dir=tmp)

            # 先模拟 Dify 500 故障，验证对话轮次不会丢失而是自动恢复回缓冲区
            def failing_handler(call: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
                return (500, {"message": "Internal Server Error"})

            plugin._build_dify_client = lambda: DifyDatasetClient(  # type: ignore[method-assign]
                base_url="https://api.dify.ai/v1",
                api_key="dataset-key",
                dataset_id="ds-id",
                session=FakeClientSession(failing_handler),  # type: ignore[arg-type]
            )

            await plugin.storage.append_turn("u99", "我周末喜欢弹吉他", "很棒的爱好！")
            ev = FakeEvent(sender_id="u99", message_str="/mem force")
            force_outputs = [out async for out in plugin.mem_force(ev)]
            self.assertTrue(any("❌" in o for o in force_outputs))
            # 验证失败后对话已恢复到缓冲区
            self.assertEqual(plugin.storage.get_buffer_count("u99"), 1)
            self.assertEqual(len(legacy_ctx.provider.calls), 1)

            # 验证 mem_clear 在远端 Dify 返回 500 时保留 document_id 映射（不产生永久孤儿文档），但清空本地未归档缓冲
            await plugin.storage.set_document_id("u99", "doc-u99-remote")
            clear_err_outputs = [out async for out in plugin.mem_clear(ev)]
            self.assertTrue(any("⚠️" in o for o in clear_err_outputs))
            self.assertEqual(plugin.storage.get_document_id("u99"), "doc-u99-remote")
            self.assertEqual(plugin.storage.get_buffer_count("u99"), 0)

            # 验证 score_threshold=0.0 且显式配置 search_method="hybrid_search" 时不会被 `or 0.45` 错误覆盖
            plugin.config["score_threshold"] = 0.0
            plugin.config["search_method"] = "hybrid_search"
            captured_Retrieve: List[Dict[str, Any]] = []

            def zero_threshold_handler(call: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
                captured_Retrieve.append(call)
                return (200, {"records": []})

            plugin._build_dify_client = lambda: DifyDatasetClient(  # type: ignore[method-assign]
                base_url="https://api.dify.ai/v1",
                api_key="dataset-key",
                dataset_id="ds-id",
                session=FakeClientSession(zero_threshold_handler),  # type: ignore[arg-type]
            )
            ev_search = FakeEvent(sender_id="u99", message_str="/mem search 吉他")
            _ = [out async for out in plugin.mem_search(ev_search, "吉他")]
            self.assertEqual(
                captured_Retrieve[-1]["json"]["retrieval_model"]["score_threshold"],
                0.0,
            )

    async def test_http_404_disambiguation_and_dataset_default_retrieval(self) -> None:
        # 1. 验证 HTTP 404 的 4 种细分场景
        # 1a) Document not found. (code="not_found") -> DifyDocumentNotFoundError
        with self.assertRaises(DifyDocumentNotFoundError):
            DifyDatasetClient._handle_http_response(
                404,
                json.dumps({"code": "not_found", "message": "Document not found.", "status": 404}),
            )

        # 1b) Document is not completed. -> DifyAPIError (不应触发 DifyDocumentNotFoundError 自动重建文档)
        with self.assertRaises(DifyAPIError) as ctx_not_completed:
            DifyDatasetClient._handle_http_response(
                404,
                json.dumps({"code": "not_found", "message": "Document is not completed.", "status": 404}),
            )
        self.assertNotIsInstance(ctx_not_completed.exception, DifyDocumentNotFoundError)
        self.assertIn("异步索引", str(ctx_not_completed.exception))

        # 1c) Dataset not found. -> DifyAPIError (不应被 delete_user_document 静默吞掉)
        with self.assertRaises(DifyAPIError) as ctx_dataset_missing:
            DifyDatasetClient._handle_http_response(
                404,
                json.dumps({"code": "not_found", "message": "Dataset not found.", "status": 404}),
            )
        self.assertNotIsInstance(ctx_dataset_missing.exception, DifyDocumentNotFoundError)
        self.assertIn("经典知识库", str(ctx_dataset_missing.exception))

        # 1d) Document is disabled. -> DifyAPIError
        with self.assertRaises(DifyAPIError) as ctx_disabled:
            DifyDatasetClient._handle_http_response(
                404,
                json.dumps({"code": "not_found", "message": "Document is disabled.", "status": 404}),
            )
        self.assertNotIsInstance(ctx_disabled.exception, DifyDocumentNotFoundError)
        self.assertIn("禁用", str(ctx_disabled.exception))

        # 2. 验证 upsert_user_memories 遇到 "Document is not completed." 时不会误调 create-by-text 重建重复文档
        calls_log: List[Dict[str, Any]] = []

        def indexing_404_handler(call: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
            calls_log.append(call)
            return (
                404,
                {"code": "not_found", "message": "Document is not completed.", "status": 404},
            )

        client_indexing = DifyDatasetClient(
            base_url="https://api.dify.ai/v1",
            api_key="dataset-key",
            dataset_id="ds-001",
            session=FakeClientSession(indexing_404_handler),  # type: ignore[arg-type]
        )
        with self.assertRaises(DifyAPIError) as ctx_upsert:
            await client_indexing.upsert_user_memories(
                user_id="u_idx",
                current_doc_id="doc-indexing-1",
                memories=[{"content": "用户正在测试异步索引竞争", "keywords": []}],
            )
        self.assertNotIsInstance(ctx_upsert.exception, DifyDocumentNotFoundError)
        # 仅调用了 1 次 /segments，绝未调用 /document/create-by-text
        self.assertEqual(len(calls_log), 1)
        self.assertIn("/segments", calls_log[0]["url"])

        # 3. 验证 delete_user_document 遇到 "Dataset not found." 时抛出异常而非返回 True
        def dataset_404_handler(call: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
            return (
                404,
                {"code": "not_found", "message": "Dataset not found.", "status": 404},
            )

        client_bad_ds = DifyDatasetClient(
            base_url="https://api.dify.ai/v1",
            api_key="dataset-key",
            dataset_id="ds-wrong",
            session=FakeClientSession(dataset_404_handler),  # type: ignore[arg-type]
        )
        with self.assertRaises(DifyAPIError):
            await client_bad_ds.delete_user_document("doc-123")

        # 4. 验证 search_method="dataset_default" 时省略 retrieval_model 以保留 Dify WebUI Rerank 配置，且在本地应用 top_k 截断
        retrieve_calls: List[Dict[str, Any]] = []

        def retrieve_handler(call: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
            retrieve_calls.append(call)
            return (
                200,
                {
                    "records": [
                        {
                            "score": 0.95,
                            "segment": {
                                "id": f"seg-{i}",
                                "document_id": "doc-u1",
                                "content": f"[USER:u1] [TIME:2026-10-30] 记忆条目 {i}",
                                "keywords": [],
                            },
                        }
                        for i in range(5)
                    ]
                },
            )

        client_ret = DifyDatasetClient(
            base_url="https://api.dify.ai/v1",
            api_key="dataset-key",
            dataset_id="ds-001",
            session=FakeClientSession(retrieve_handler),  # type: ignore[arg-type]
        )
        res_default = await client_ret.retrieve_memories(
            query="测试Rerank默认配置",
            user_id="u1",
            document_id="doc-u1",
            top_k=2,
            score_threshold=0.5,
            search_method="dataset_default",
        )
        self.assertNotIn("retrieval_model", retrieve_calls[-1]["json"])
        self.assertEqual(len(res_default), 2)

        # 显式指定 search_method="semantic_search" 时则包含 retrieval_model
        await client_ret.retrieve_memories(
            query="测试显式检索模式",
            user_id="u1",
            document_id="doc-u1",
            top_k=3,
            score_threshold=0.6,
            search_method="semantic_search",
        )
        self.assertIn("retrieval_model", retrieve_calls[-1]["json"])
        self.assertEqual(
            retrieve_calls[-1]["json"]["retrieval_model"]["search_method"],
            "semantic_search",
        )

    async def test_edge_cases_short_query_empty_summary_and_mem_cmd_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # 当 LLM 返回 [] 时，应正常清空缓冲且不请求 Dify 创建文档
            ctx = FakeContextV4(summary_reply="[]")
            config = {
                "dify_base_url": "https://api.dify.ai/v1",
                "dify_dataset_api_key": "dataset-key",
                "dify_dataset_id": "ds-id",
                "summary_turns": 2,
                "min_query_length": 3,
            }
            plugin = DifyMemoryPlugin(context=ctx, config=config, data_dir=tmp)
            await plugin.storage.set_document_id("u100", "doc-existing")

            fake_session = FakeClientSession(lambda c: (200, {"records": []}))
            plugin._build_dify_client = lambda: DifyDatasetClient(  # type: ignore[method-assign]
                base_url="https://api.dify.ai/v1",
                api_key="dataset-key",
                dataset_id="ds-id",
                session=fake_session,  # type: ignore[arg-type]
            )

            # 1. 短于 min_query_length (3) 的消息 "早" 不触发 retrieve，但仍进入对话缓冲
            ev_short = FakeEvent(sender_id="u100", message_str="早", umo="qq:group:1")
            req_short = FakeProviderRequest(prompt="早")
            await plugin.on_llm_request(ev_short, req_short)
            self.assertEqual(len(fake_session.calls), 0)
            await plugin.on_llm_response(ev_short, FakeLLMResponse("早上好！"))
            self.assertEqual(plugin.storage.get_buffer_count("u100"), 1)

            # 2. /mem 指令本身不应被 on_llm_request / on_llm_response 计入对话缓冲
            ev_cmd = FakeEvent(sender_id="u100", message_str="/mem status", umo="qq:group:1")
            await plugin.on_llm_request(ev_cmd, FakeProviderRequest(prompt="/mem status"))
            await plugin.on_llm_response(ev_cmd, FakeLLMResponse("status reply"))
            self.assertEqual(plugin.storage.get_buffer_count("u100"), 1)

            # 3. 第 2 轮水群凑满 2 轮，LLM 返回 []，验证不调用 Dify 写入且缓冲区清零
            ev_chat2 = FakeEvent(sender_id="u100", message_str="哈哈哈好滴", umo="qq:group:1")
            await plugin.on_llm_request(ev_chat2, FakeProviderRequest(prompt="哈哈哈好滴"))
            # 此处长度 >= 3 会触发 1 次 retrieve
            self.assertEqual(len(fake_session.calls), 1)
            await plugin.on_llm_response(ev_chat2, FakeLLMResponse("哈哈~"))
            if plugin._bg_tasks:
                await asyncio.gather(*list(plugin._bg_tasks))
            self.assertEqual(plugin.storage.get_buffer_count("u100"), 0)
            # 除那 1 次 retrieve 外，没有调用 create-by-text 或 segments
            self.assertEqual(len(fake_session.calls), 1)

    async def test_real_loopback_http_server(self) -> None:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading

        received_requests: List[Dict[str, Any]] = []

        class MockDifyHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length).decode("utf-8") if length else "{}"
                received_requests.append(
                    {
                        "method": "POST",
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "body": json.loads(raw_body),
                    }
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if self.path.endswith("/document/create-by-text"):
                    resp = {"document": {"id": "real-http-doc-1"}}
                else:
                    resp = {"data": [{"id": "seg-real-1"}]}
                self.wfile.write(json.dumps(resp).encode("utf-8"))

            def do_DELETE(self) -> None:
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"code": "not_found", "message": "Document not found."}).encode("utf-8")
                )

        server = HTTPServer(("127.0.0.1", 0), MockDifyHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = DifyDatasetClient(
                base_url=f"http://127.0.0.1:{port}/v1",
                api_key="dataset-real-key",
                dataset_id="ds-real",
            )
            doc_id = await client.ensure_user_document("u_http", "测试真实HTTP通信")
            self.assertEqual(doc_id, "real-http-doc-1")
            self.assertEqual(received_requests[0]["auth"], "Bearer dataset-real-key")

            # 删除时即便返回 404 (Document not found.) 也应视为成功返回 True
            deleted = await client.delete_user_document("real-http-doc-1")
            self.assertTrue(deleted)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
