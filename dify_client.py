from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    import aiohttp  # type: ignore[import-not-found]
except ImportError:
    aiohttp = None  # type: ignore[assignment]

try:
    from astrbot.api import logger as astrbot_logger
except ImportError:
    astrbot_logger = logging.getLogger("astrbot_plugin_dify_memory")

SEGMENT_SEPARATOR = "\n\n===MEM_SEG===\n\n"
USER_TAG_PATTERN = re.compile(r"^\[USER:([^\]]+)\]\s*(?:\[TIME:([^\]]+)\]\s*)?")


class DifyAPIError(Exception):
    """Dify 知识库 API 请求异常。"""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        error_code: str = "",
        response_body: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.response_body = response_body


class DifyDocumentNotFoundError(DifyAPIError):
    """远端 Dify Document 不存在或已被删除（404）。"""


def format_memory_content(
    user_id: str, content: str, date_str: Optional[str] = None
) -> str:
    """将记忆内容标准化为携带 [USER:{user_id}] [TIME:YYYY-MM-DD] 前缀的文本。"""
    uid = str(user_id).strip()
    text = content.strip()
    if not text:
        return ""
    user_tag = f"[USER:{uid}]"
    if text.startswith(user_tag):
        return text
    # 若带有其他或残缺的 USER 标签，先剥离再统一加上当前用户的标签
    text = USER_TAG_PATTERN.sub("", text).strip()
    day = date_str or datetime.now().strftime("%Y-%m-%d")
    return f"[USER:{uid}] [TIME:{day}] {text}"


KEYWORDS_SUFFIX_PATTERN = re.compile(r"\s*\[KEYWORDS:[^\]]*\]\s*$")


def strip_memory_prefix(content: str) -> Tuple[str, str]:
    """从分段内容中剥离 [USER:xxx] [TIME:xxx] 前缀与尾部 [KEYWORDS:...] 标记，返回 (time_str, clean_text)。"""
    text = content.strip()
    match = USER_TAG_PATTERN.match(text)
    if not match:
        clean = KEYWORDS_SUFFIX_PATTERN.sub("", text).strip()
        return ("", clean)
    time_str = (match.group(2) or "").strip()
    clean_text = text[match.end() :].strip()
    clean_text = KEYWORDS_SUFFIX_PATTERN.sub("", clean_text).strip()
    return (time_str, clean_text)


def normalize_keywords(keywords: Any, max_count: int = 10) -> List[str]:
    """清洗并去重关键词列表。"""
    if not keywords:
        return []
    if isinstance(keywords, str):
        raw_items = [k.strip() for k in re.split(r"[,，、\s]+", keywords) if k.strip()]
    elif isinstance(keywords, (list, tuple, set)):
        raw_items = [str(k).strip() for k in keywords if str(k).strip()]
    else:
        return []

    seen = set()
    result: List[str] = []
    for item in raw_items:
        if item not in seen:
            seen.add(item)
            result.append(item)
        if len(result) >= max_count:
            break
    return result


class DifyDatasetClient:
    """Dify 知识库（Dataset）异步客户端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        dataset_id: str,
        timeout: int = 15,
        session: Optional[Any] = None,
    ) -> None:
        self.base_url = (base_url or "https://api.dify.ai/v1").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.dataset_id = (dataset_id or "").strip()
        self.timeout = max(3, int(timeout))
        self._external_session = session

    @property
    def is_configured(self) -> bool:
        """检查 Dify 知识库必填配置是否完整。"""
        return bool(self.base_url and self.api_key and self.dataset_id)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _handle_http_response(status: int, raw_text: str) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        if raw_text.strip():
            try:
                parsed = json.loads(raw_text)
                if isinstance(parsed, dict):
                    data = parsed
            except Exception:
                pass

        err_code = str(data.get("code") or "").strip()
        raw_msg = str(data.get("message") or "").strip()
        msg_lower = raw_msg.lower()

        if err_code == "document_not_found":
            raise DifyDocumentNotFoundError(
                raw_msg or f"Dify Document 不存在 (HTTP {status})",
                status_code=status,
                error_code=err_code,
                response_body=raw_text,
            )

        if status == 404:
            if "not completed" in msg_lower:
                raise DifyAPIError(
                    f"远端文档尚在异步索引队列中，请稍后重试 ({raw_msg})",
                    status_code=status,
                    error_code=err_code or "not_found",
                    response_body=raw_text,
                )
            if "dataset not found" in msg_lower or "dataset not exists" in msg_lower:
                raise DifyAPIError(
                    f"Dify 知识库不存在，请检查 dify_dataset_id 是否为有效的经典知识库 UUID ({raw_msg})",
                    status_code=status,
                    error_code=err_code or "not_found",
                    response_body=raw_text,
                )
            if "disabled" in msg_lower:
                raise DifyAPIError(
                    f"远端文档已被禁用 ({raw_msg})",
                    status_code=status,
                    error_code=err_code or "not_found",
                    response_body=raw_text,
                )
            if (
                "document not found" in msg_lower
                or "document not exists" in msg_lower
                or "document does not exist" in msg_lower
            ):
                raise DifyDocumentNotFoundError(
                    raw_msg or f"Dify Document 不存在 (HTTP {status})",
                    status_code=status,
                    error_code=err_code or "document_not_found",
                    response_body=raw_text,
                )

        if status >= 400:
            msg = (
                raw_msg
                or raw_text[:300]
                or f"Dify API 请求失败 (HTTP {status})"
            )
            raise DifyAPIError(
                msg,
                status_code=status,
                error_code=err_code,
                response_body=raw_text,
            )

        return data

    def _sync_urllib_request(
        self,
        method: str,
        url: str,
        json_payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, str]:
        full_url = url
        if params:
            query_str = urllib.parse.urlencode(params)
            sep = "&" if "?" in full_url else "?"
            full_url = f"{full_url}{sep}{query_str}"

        body_bytes = (
            json.dumps(json_payload, ensure_ascii=False).encode("utf-8")
            if json_payload is not None
            else None
        )
        req = urllib.request.Request(
            full_url,
            data=body_bytes,
            headers=self._headers(),
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                raw_text = resp.read().decode("utf-8", errors="replace")
                return (status, raw_text)
        except urllib.error.HTTPError as err:
            raw_text = err.read().decode("utf-8", errors="replace") if err.fp else ""
            return (err.code, raw_text)

    async def _request(
        self,
        method: str,
        path: str,
        json_payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.is_configured:
            raise DifyAPIError(
                "Dify 知识库未完成配置（缺少 dify_dataset_api_key 或 dify_dataset_id）"
            )

        url = f"{self.base_url}/{path.lstrip('/')}"
        client_timeout = (
            aiohttp.ClientTimeout(total=self.timeout) if aiohttp is not None else self.timeout
        )

        async def _do_call(sess: Any) -> Dict[str, Any]:
            async with sess.request(
                method=method.upper(),
                url=url,
                headers=self._headers(),
                json=json_payload,
                params=params,
                timeout=client_timeout,
            ) as resp:
                raw_text = await resp.text()
                return self._handle_http_response(resp.status, raw_text)

        if self._external_session is not None:
            return await _do_call(self._external_session)

        if aiohttp is not None:
            async with aiohttp.ClientSession() as session:
                return await _do_call(session)

        status, raw_text = await asyncio.to_thread(
            self._sync_urllib_request,
            method,
            url,
            json_payload,
            params,
        )
        return self._handle_http_response(status, raw_text)

    async def ensure_user_document(
        self, user_id: str, first_segment_text: str | List[str]
    ) -> str:
        """
        调用 create-by-text 为指定 user_id 创建专属记忆文档 mem_user_{user_id}.txt。
        支持传入单条文本或多条文本列表（自动使用 SEGMENT_SEPARATOR 拼接一次性建档切分）。
        返回新建的 document_id。
        """
        uid = str(user_id).strip()
        if isinstance(first_segment_text, list):
            parts = [
                format_memory_content(uid, seg)
                for seg in first_segment_text
                if str(seg).strip()
            ]
            parts = [p for p in parts if p]
            formatted_text = SEGMENT_SEPARATOR.join(parts)
        else:
            formatted_text = format_memory_content(uid, first_segment_text)

        if not formatted_text:
            raise ValueError("创建用户记忆文档的首条内容不能为空")

        payload = {
            "name": f"mem_user_{uid}.txt",
            "text": formatted_text,
            "indexing_technique": "high_quality",
            "process_rule": {
                "mode": "custom",
                "rules": {
                    "pre_processing_rules": [
                        {"id": "remove_extra_spaces", "enabled": True},
                        {"id": "remove_urls_emails", "enabled": False},
                    ],
                    "segmentation": {
                        "separator": SEGMENT_SEPARATOR,
                        "max_tokens": 1000,
                    },
                },
            },
        }
        data = await self._request(
            "POST",
            f"/datasets/{self.dataset_id}/document/create-by-text",
            json_payload=payload,
        )
        doc_info = data.get("document")
        doc_id = ""
        if isinstance(doc_info, dict):
            doc_id = str(doc_info.get("id") or "").strip()
        if not doc_id:
            doc_id = str(data.get("id") or "").strip()
        if not doc_id:
            raise DifyAPIError(
                f"Dify create-by-text 响应缺少 document.id: {data}"
            )
        return doc_id

    async def add_memory_segments(
        self, document_id: str, segments: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """向指定 document_id 批量追加记忆分段（Segments）。"""
        doc_id = str(document_id).strip()
        if not doc_id:
            raise ValueError("document_id 不能为空")

        cleaned_segments: List[Dict[str, Any]] = []
        for seg in segments:
            content = str(seg.get("content") or "").strip()
            if not content:
                continue
            item: Dict[str, Any] = {"content": content}
            kws = normalize_keywords(seg.get("keywords"))
            if kws:
                item["keywords"] = kws
            cleaned_segments.append(item)

        if not cleaned_segments:
            return {"data": []}

        payload = {"segments": cleaned_segments}
        return await self._request(
            "POST",
            f"/datasets/{self.dataset_id}/documents/{doc_id}/segments",
            json_payload=payload,
        )

    async def upsert_user_memories(
        self,
        user_id: str,
        current_doc_id: Optional[str],
        memories: List[Dict[str, Any]],
        date_str: Optional[str] = None,
    ) -> Tuple[str, int]:
        """
        将一批记忆写入指定用户的 Dify Document：
        - 若无 document_id，则调用 ensure_user_document 通过 SEGMENT_SEPARATOR 一次性写入整批分段，
          彻底规避新建文档处于异步索引队列时立即调用 /segments 报 400 的时序竞争；
        - 若已有 document_id，则调用 add_memory_segments 追加；
        - 若远端返回 404（文档被手动删除），自动重建 Document 并返回新 document_id。
        返回 (document_id, 写入分段数量)。
        """
        uid = str(user_id).strip()
        formatted_segments: List[Dict[str, Any]] = []
        for mem in memories:
            raw_content = str(mem.get("content") or "").strip()
            if not raw_content:
                continue
            formatted_text = format_memory_content(uid, raw_content, date_str=date_str)
            if not formatted_text:
                continue
            kws = normalize_keywords(mem.get("keywords"))
            formatted_segments.append(
                {
                    "content": formatted_text,
                    "keywords": kws,
                }
            )

        if not formatted_segments:
            return (current_doc_id or "", 0)

        async def _create_initial_doc() -> str:
            initial_texts: List[str] = []
            for seg in formatted_segments:
                text = seg["content"]
                kws = seg.get("keywords") or []
                if kws and "[KEYWORDS:" not in text:
                    text = f"{text} [KEYWORDS:{','.join(kws)}]"
                initial_texts.append(text)
            return await self.ensure_user_document(uid, initial_texts)

        if not current_doc_id:
            doc_id = await _create_initial_doc()
            return (doc_id, len(formatted_segments))

        try:
            await self.add_memory_segments(current_doc_id, formatted_segments)
            return (current_doc_id, len(formatted_segments))
        except DifyDocumentNotFoundError:
            astrbot_logger.warning(
                f"[DifyMemory] 用户 {uid} 的远端文档 ({current_doc_id}) 不存在，正在自动重建..."
            )
            doc_id = await _create_initial_doc()
            return (doc_id, len(formatted_segments))

    async def retrieve_memories(
        self,
        query: str,
        user_id: str,
        document_id: Optional[str] = None,
        top_k: int = 5,
        score_threshold: float = 0.45,
        search_method: str = "dataset_default",
    ) -> List[Dict[str, Any]]:
        """
        从 Dify 知识库检索与当前用户输入相关的长期记忆，并严格过滤只属于该 user_id 的分段。
        当 search_method 为 "dataset_default"（或留空）时，请求体不携带 retrieval_model，
        直接沿用用户在 Dify 知识库 WebUI 中配置的检索模式、Rerank 模型与参数。
        """
        uid = str(user_id).strip()
        # Dify retrieve 接口限制 query 最大 250 字符
        clean_query = (query or "").strip()[:250]
        if not clean_query or not uid:
            return []

        effective_top_k = max(1, min(int(top_k), 20))
        effective_threshold = max(0.0, min(float(score_threshold), 1.0))

        payload: Dict[str, Any] = {
            "query": clean_query,
        }
        method_norm = (search_method or "").strip()
        if method_norm and method_norm != "dataset_default":
            payload["retrieval_model"] = {
                "search_method": method_norm,
                "reranking_enable": False,
                "top_k": effective_top_k,
                "score_threshold_enabled": True,
                "score_threshold": effective_threshold,
            }

        data = await self._request(
            "POST",
            f"/datasets/{self.dataset_id}/retrieve",
            json_payload=payload,
        )

        records = data.get("records")
        if not isinstance(records, list):
            return []

        expected_doc_id = (document_id or "").strip()
        matched: List[Dict[str, Any]] = []

        for rec in records:
            if not isinstance(rec, dict):
                continue
            segment = rec.get("segment")
            if not isinstance(segment, dict):
                continue

            content = str(segment.get("content") or "").strip()
            if not content:
                continue

            seg_doc_id = str(
                segment.get("document_id")
                or (segment.get("document") or {}).get("id")
                or ""
            ).strip()

            score_val = rec.get("score")
            score = float(score_val) if isinstance(score_val, (int, float)) else None
            if score is not None and score < effective_threshold:
                continue

            # 跨用户与过期文档隔离校验：
            # 1. 若同时指定了 expected_doc_id 且返回分段带有 seg_doc_id，必须与当前绑定的 expected_doc_id 一致（防止已清理/旧孤儿文档污染）
            if expected_doc_id and seg_doc_id and seg_doc_id != expected_doc_id:
                continue

            # 2. 若内容带有 [USER:xxx] 标签，必须等于当前用户 [USER:{uid}]
            # 3. 若内容未带 [USER:xxx] 标签，则必须与当前用户的 expected_doc_id 完全一致
            tag_match = USER_TAG_PATTERN.match(content)
            if tag_match:
                tagged_uid = (tag_match.group(1) or "").strip()
                if tagged_uid != uid:
                    continue
            else:
                if not expected_doc_id or seg_doc_id != expected_doc_id:
                    continue

            time_str, clean_text = strip_memory_prefix(content)
            matched.append(
                {
                    "id": str(segment.get("id") or ""),
                    "document_id": seg_doc_id,
                    "content": content,
                    "clean_content": clean_text,
                    "time": time_str,
                    "keywords": normalize_keywords(segment.get("keywords")),
                    "score": score,
                }
            )
            if len(matched) >= effective_top_k:
                break

        return matched

    async def list_user_segments(
        self, document_id: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """列出指定用户文档下的记忆分段列表。"""
        doc_id = str(document_id).strip()
        if not doc_id:
            return []
        data = await self._request(
            "GET",
            f"/datasets/{self.dataset_id}/documents/{doc_id}/segments",
            params={"limit": max(1, min(int(limit), 100))},
        )
        items = data.get("data")
        return items if isinstance(items, list) else []

    async def delete_user_document(self, document_id: str) -> bool:
        """删除指定用户的远端 Dify Document。若远端已不存在也视为成功。"""
        doc_id = str(document_id).strip()
        if not doc_id:
            return False
        try:
            await self._request(
                "DELETE",
                f"/datasets/{self.dataset_id}/documents/{doc_id}",
            )
            return True
        except DifyDocumentNotFoundError:
            return True
