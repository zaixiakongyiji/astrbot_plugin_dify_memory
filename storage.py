from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from astrbot.api import logger as astrbot_logger
    from astrbot.api.star import StarTools
except ImportError:
    astrbot_logger = logging.getLogger("astrbot_plugin_dify_memory")
    StarTools = None  # type: ignore[assignment]


def resolve_default_data_dir(plugin_name: str = "astrbot_plugin_dify_memory") -> Path:
    """获取插件持久化目录，优先使用 AstrBot 规范的 StarTools.get_data_dir。"""
    if StarTools is not None:
        try:
            data_dir = StarTools.get_data_dir(plugin_name)
            if data_dir:
                return Path(data_dir)
        except TypeError:
            try:
                data_dir = StarTools.get_data_dir()
                if data_dir:
                    return Path(data_dir)
            except Exception:
                pass
        except Exception:
            pass
    return Path(__file__).resolve().parent / "data"


class MemoryStorage:
    """管理 user_id <-> document_id 映射以及未归档对话轮次缓冲区。"""

    def __init__(self, data_dir: Optional[Path | str] = None) -> None:
        self.data_dir: Path = (
            Path(data_dir) if data_dir is not None else resolve_default_data_dir()
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.docs_file: Path = self.data_dir / "user_documents.json"
        self.buffers_file: Path = self.data_dir / "turn_buffers.json"

        self._lock = asyncio.Lock()
        self._user_locks: Dict[str, asyncio.Lock] = {}

        self._user_docs: Dict[str, str] = self._load_json(self.docs_file, default={})
        self._turn_buffers: Dict[str, List[Dict[str, str]]] = self._load_json(
            self.buffers_file, default={}
        )

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            content = path.read_text(encoding="utf-8")
            if not content.strip():
                return default
            data = json.loads(content)
            return data if isinstance(data, type(default)) else default
        except Exception as exc:
            astrbot_logger.warning(f"[DifyMemory] 读取本地状态文件失败 ({path}): {exc}")
            return default

    @staticmethod
    def _save_json(path: Path, data: Any) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp_path.replace(path)
        except Exception as exc:
            astrbot_logger.error(f"[DifyMemory] 写入本地状态文件失败 ({path}): {exc}")

    def get_user_lock(self, user_id: str) -> asyncio.Lock:
        """获取指定用户的异步锁，防止同一用户并发创建或修改 Dify 文档。"""
        uid = str(user_id)
        if uid not in self._user_locks:
            self._user_locks[uid] = asyncio.Lock()
        return self._user_locks[uid]

    def get_document_id(self, user_id: str) -> Optional[str]:
        """获取用户绑定的 Dify Document ID。"""
        doc_id = self._user_docs.get(str(user_id))
        return doc_id.strip() if isinstance(doc_id, str) and doc_id.strip() else None

    async def set_document_id(self, user_id: str, document_id: str) -> None:
        """绑定用户的 Dify Document ID 并持久化。"""
        uid = str(user_id)
        async with self._lock:
            self._user_docs[uid] = document_id.strip()
            self._save_json(self.docs_file, self._user_docs)

    async def remove_document_id(self, user_id: str) -> Optional[str]:
        """移除用户的 Dify Document ID 映射。"""
        uid = str(user_id)
        async with self._lock:
            removed = self._user_docs.pop(uid, None)
            self._save_json(self.docs_file, self._user_docs)
            return removed

    def get_buffer_count(self, user_id: str) -> int:
        """返回用户当前已累计未归档的对话轮数。"""
        return len(self._turn_buffers.get(str(user_id), []))

    async def append_turn(
        self,
        user_id: str,
        user_msg: str,
        assistant_msg: str,
        timestamp: Optional[str] = None,
    ) -> int:
        """向用户的未归档缓冲区追加一轮对话，返回当前累计轮数。"""
        uid = str(user_id)
        ts = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        turn_entry = {
            "user": user_msg.strip(),
            "assistant": assistant_msg.strip(),
            "timestamp": ts,
        }
        async with self._lock:
            buf = self._turn_buffers.setdefault(uid, [])
            buf.append(turn_entry)
            count = len(buf)
            self._save_json(self.buffers_file, self._turn_buffers)
            return count

    async def pop_turns_if_ready(
        self, user_id: str, threshold: int
    ) -> List[Dict[str, str]]:
        """若用户累计对话轮数达到阈值，原子弹出当前已累计的对话轮次并持久化。"""
        uid = str(user_id)
        effective_threshold = max(1, int(threshold))
        async with self._lock:
            buf = self._turn_buffers.get(uid, [])
            if len(buf) < effective_threshold:
                return []
            popped = list(buf)
            self._turn_buffers[uid] = []
            self._save_json(self.buffers_file, self._turn_buffers)
            return popped

    async def pop_all_turns(self, user_id: str) -> List[Dict[str, str]]:
        """原子弹出指定用户缓冲区中的所有对话轮次（用于强制立即归档）。"""
        uid = str(user_id)
        async with self._lock:
            buf = self._turn_buffers.pop(uid, [])
            if buf:
                self._save_json(self.buffers_file, self._turn_buffers)
            return list(buf)

    async def restore_turns(
        self, user_id: str, turns: List[Dict[str, str]], max_keep: int = 30
    ) -> None:
        """当后台总结或写入失败时，将未归档的对话轮次恢复至缓冲区头部。"""
        if not turns:
            return
        uid = str(user_id)
        async with self._lock:
            current = self._turn_buffers.get(uid, [])
            merged = list(turns) + current
            if max_keep > 0 and len(merged) > max_keep:
                merged = merged[-max_keep:]
            self._turn_buffers[uid] = merged
            self._save_json(self.buffers_file, self._turn_buffers)

    async def clear_user_state(self, user_id: str) -> Optional[str]:
        """清空指定用户的本地缓冲与文档映射，返回旧的 document_id（若有）。"""
        uid = str(user_id)
        async with self._lock:
            old_doc_id = self._user_docs.pop(uid, None)
            self._turn_buffers.pop(uid, None)
            self._save_json(self.docs_file, self._user_docs)
            self._save_json(self.buffers_file, self._turn_buffers)
            return old_doc_id
