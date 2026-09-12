# -*- coding: utf-8 -*-
"""上传文件的限额、格式验证与原子落盘。"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
import zipfile
from pathlib import Path

from fastapi import UploadFile


##### 上传约束板块 #####


DOCX_LIMIT = int(os.environ.get("SCHOLAR_DOCX_MAX_BYTES", 100 * 1024 * 1024))
BIB_LIMIT = int(os.environ.get("SCHOLAR_BIB_MAX_BYTES", 10 * 1024 * 1024))
MAP_LIMIT = int(os.environ.get("SCHOLAR_MAP_MAX_BYTES", 2 * 1024 * 1024))


class UploadValidationError(ValueError):
    """上传内容不符合本接口的类型或安全约束。"""


async def save_upload(
    upload: UploadFile, destination: Path, *, limit: int
) -> tuple[int, str]:
    """分块读取并原子替换，避免半文件被后续任务读取。"""
    temporary = destination.with_name(f".upload-{uuid.uuid4().hex}")
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("xb") as target:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise UploadValidationError(f"文件超过上传上限 {limit} 字节")
                digest.update(chunk)
                target.write(chunk)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    return size, digest.hexdigest()


def validate_docx(path: Path) -> None:
    if path.suffix.lower() != ".docx":
        raise UploadValidationError("仅支持 .docx 格式的 Word 文档")
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise UploadValidationError("上传文件不是有效的 DOCX 压缩包") from exc
    required = {"[Content_Types].xml", "word/document.xml"}
    if not required.issubset(names):
        raise UploadValidationError("DOCX 缺少必要的 Word XML 文件")


def validate_json_mapping(path: Path) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UploadValidationError("引用映射必须是 UTF-8 JSON") from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise UploadValidationError("引用映射必须是字符串到字符串的 JSON 对象")
