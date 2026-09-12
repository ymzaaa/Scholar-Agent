# -*- coding: utf-8 -*-
"""修改型 Agent 的动作日志与幂等文件恢复。"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path



##### 通用单区域文件事务板块 #####


def _filesystem_path(path: Path) -> Path:
    """保留完整绝对路径与哈希，Windows 使用扩展表示访问深目录临时文件。"""
    if os.name != 'nt':
        return path
    absolute = str(path.resolve())
    if not absolute.startswith('\\\\?\\'):
        absolute = ('\\\\?\\UNC\\' + absolute[2:] if absolute.startswith('\\\\')
                    else '\\\\?\\' + absolute)
    return Path(absolute)


class FileActionJournal:
    """保存局部前后片段及整文件哈希；每次写入和回滚都验证当前字节。"""

    def __init__(self, root: Path, journal_root: Path, run_id: str, *, generation_id: str):
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise ValueError("运行日志标识无效。")
        self.root = root.resolve()
        self.generation_id = generation_id
        self.directory = _filesystem_path(journal_root.resolve() / run_id / "actions")
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, action_id: str) -> Path:
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]+", action_id):
            raise ValueError("动作日志标识无效。")
        return self.directory / f"{action_id}.json"

    @staticmethod
    def _sha(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    @staticmethod
    def _atomic(path: Path, content: bytes) -> None:
        path = _filesystem_path(path)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        except BaseException as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                exc.add_note(f"临时动作文件清理失败：{cleanup_error}")
            raise

    def _write(self, path: Path, payload: dict) -> None:
        self._atomic(path, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def _target(self, relative: str) -> Path:
        from .patching import writable_path
        return writable_path(self.root, relative, self.generation_id)

    def _replace_target(self, target: Path, content: bytes) -> None:
        self._atomic(target, content)

    def apply(self, action_id: str, prepared: dict, *, proposal: dict) -> dict:
        path = self._path(action_id)
        target = self._target(prepared["relative_path"])
        before, after = prepared["before"], prepared["after"]
        if path.exists():
            raise ActionJournalError("该动作已经存在文件日志，不能作为新动作再次写入。")
        if target.read_bytes() != before:
            raise ActionJournalError("写入前文件与已验证证据不一致。")
        # 公共前后缀仅用于精确字节回滚，不扩大模型的区域写入权限。
        offset = 0
        while offset < min(len(before), len(after)) and before[offset] == after[offset]:
            offset += 1
        suffix = 0
        while suffix < min(len(before), len(after)) - offset and before[-suffix-1] == after[-suffix-1]:
            suffix += 1
        import base64
        payload = {
            "action_id": action_id, "sequence": len(list(self.directory.glob("*.json"))),
            "relative_path": prepared["relative_path"], "unit_id": prepared["unit_id"],
            "status": "prepared", "before_sha256": self._sha(before), "after_sha256": self._sha(after),
            "offset": offset,
            "old_bytes": base64.b64encode(before[offset:len(before)-suffix if suffix else len(before)]).decode(),
            "new_bytes": base64.b64encode(after[offset:len(after)-suffix if suffix else len(after)]).decode(),
            "old_fragment": prepared["old_fragment"], "new_fragment": prepared["new_fragment"],
            "proposal": proposal,
        }
        if "confirmed_replacement" in prepared:
            payload["confirmed_replacement"] = prepared["confirmed_replacement"]
        self._write(path, payload)
        self._replace_target(target, after)
        if self._sha(target.read_bytes()) != payload["after_sha256"]:
            raise ActionJournalError("动作写入后字节校验失败。")
        payload["status"] = "applied"
        self._write(path, payload)
        return {"action_id": action_id, "unit_id": prepared["unit_id"], "changed_files": [prepared["relative_path"]]}

    def recover(self, proposal: dict) -> dict | None:
        """恢复已预写日志的同一动作；已提交的文件只核验，不再次写入。"""
        import base64
        path = self._path(proposal["action_id"])
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("proposal") != proposal or payload["status"] not in {"prepared", "applied"}:
            raise ActionJournalError("恢复提案与动作日志不一致，或动作已回滚。")
        target = self._target(payload["relative_path"])
        current = target.read_bytes()
        digest = self._sha(current)
        if digest != payload["after_sha256"]:
            if digest != payload["before_sha256"]:
                raise ActionJournalError("中断后的文件既不是动作前也不是动作后状态。")
            old, new = (base64.b64decode(payload[key], validate=True) for key in ("old_bytes", "new_bytes"))
            offset = payload["offset"]
            updated = current[:offset] + new + current[offset+len(old):]
            if self._sha(updated) != payload["after_sha256"]:
                raise ActionJournalError("日志不能重建已经验证的动作结果。")
            self._replace_target(target, updated)
        payload["status"] = "applied"
        self._write(path, payload)
        return {"action_id": proposal["action_id"], "unit_id": payload["unit_id"],
                "changed_files": [payload["relative_path"]], "recovered": True}

    def entries(self) -> list[dict]:
        items = [json.loads(path.read_text(encoding="utf-8")) for path in self.directory.glob("*.json")]
        return sorted(items, key=lambda item: item["sequence"])

    def rollback(self, action_id: str) -> None:
        import base64
        path = self._path(action_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["status"] == "rolled_back":
            return
        target = self._target(payload["relative_path"])
        current = target.read_bytes()
        digest = self._sha(current)
        if digest != payload["before_sha256"]:
            if digest != payload["after_sha256"]:
                raise ActionJournalError("文件已被其他操作改变，不能安全回滚。")
            offset = payload["offset"]
            old, new = (base64.b64decode(payload[key], validate=True) for key in ("old_bytes", "new_bytes"))
            restored = current[:offset] + old + current[offset+len(new):]
            if self._sha(restored) != payload["before_sha256"]:
                raise ActionJournalError("回滚片段不能重建原文件。")
            self._replace_target(target, restored)
        payload["status"] = "rolled_back"
        self._write(path, payload)

    def rollback_all(self) -> None:
        for item in reversed(self.entries()):
            self.rollback(item["action_id"])


##### 文件恢复异常板块 #####


class ActionJournalError(RuntimeError):
    """文件状态无法证明为动作前或动作后时拒绝继续。"""
