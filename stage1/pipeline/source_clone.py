# -*- coding: utf-8 -*-
"""从可信 LaTeX 源码包安全建立用户反馈候选工作副本。"""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from pipeline.source_package import allowed_source_path
from pipeline.workspace import GENERATION_MARKER, WorkspaceSafetyError, _write_json


##### 克隆约束板块 #####


MAX_SOURCE_FILES = 5000
MAX_SINGLE_FILE_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024


class SourceCloneError(WorkspaceSafetyError):
    """父源码包不可信或候选目录不满足隔离要求。"""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    relative = PurePosixPath(normalized)
    if (
        not relative.parts or not normalized or normalized.startswith("/") or relative.is_absolute()
        or ".." in relative.parts or ":" in relative.parts[0]
    ):
        raise SourceCloneError(f"源码包包含危险路径：{name!r}")
    if not allowed_source_path(relative):
        raise SourceCloneError(f"源码包包含非白名单文件：{name!r}")
    return relative


def _validate_entry(info: zipfile.ZipInfo) -> PurePosixPath:
    if info.is_dir():
        raise SourceCloneError("源码包不应包含独立目录条目。")
    if info.flag_bits & 0x1:
        raise SourceCloneError("源码包不允许加密条目。")
    file_type = (info.external_attr >> 16) & 0o170000
    if file_type == stat.S_IFLNK:
        raise SourceCloneError("源码包不允许符号链接。")
    if info.file_size > MAX_SINGLE_FILE_BYTES:
        raise SourceCloneError("源码包单文件超过安全上限。")
    return _safe_relative(info.filename)


##### 安全克隆板块 #####


def clone_latex_source(
    archive_path: str | Path,
    staging_dir: str | Path,
    *,
    expected_size: int,
    expected_sha256: str,
    report_path: str | Path,
    parent_version_id: str,
    candidate_version_id: str,
) -> dict[str, Any]:
    """校验系统产出的 zip 后流式复制；不使用 ZipFile.extract。"""
    archive = Path(archive_path).resolve()
    staging = Path(staging_dir).resolve()
    marker = staging / GENERATION_MARKER
    if not archive.is_file() or archive.stat().st_size != expected_size:
        raise SourceCloneError("父源码包缺失或大小与产物清单不一致。")
    if _sha256_file(archive) != expected_sha256:
        raise SourceCloneError("父源码包哈希与产物清单不一致。")
    if not staging.is_dir() or not marker.is_file():
        raise SourceCloneError("候选目录不是带所有权标记的系统 staging。")
    try:
        ownership = json.loads(marker.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise SourceCloneError('候选目录标记不可读。') from exc
    if ownership.get('status') != 'staging' or ownership.get('generation_id') != candidate_version_id:
        raise SourceCloneError('候选目录标记状态或 generation ID 不匹配。')
    unexpected = [item.name for item in staging.iterdir() if item.name != GENERATION_MARKER]
    if unexpected:
        raise SourceCloneError(f"候选目录在克隆前必须为空：{unexpected[:10]}")

    records: list[dict[str, Any]] = []
    created: list[Path] = []
    seen: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(archive) as source:
            infos = source.infolist()
            if not infos or len(infos) > MAX_SOURCE_FILES:
                raise SourceCloneError("父源码包为空或文件数量超过安全上限。")
            validated: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
            for info in infos:
                relative = _validate_entry(info)
                collision_key = relative.as_posix().casefold()
                if collision_key in seen:
                    raise SourceCloneError("父源码包包含大小写冲突或重复路径。")
                seen.add(collision_key)
                total += info.file_size
                if total > MAX_EXPANDED_BYTES:
                    raise SourceCloneError("父源码包解压总大小超过安全上限。")
                validated.append((info, relative))
            for info, relative in validated:
                target = staging.joinpath(*relative.parts).resolve()
                if staging not in target.parents:
                    raise SourceCloneError("源码条目逃逸候选目录。")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(info) as reader, target.open("xb") as writer:
                    created.append(target)
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                records.append({
                    "relative_path": relative.as_posix(),
                    "size": target.stat().st_size,
                })
        manifest = {
            "schema_version": "1.0.0",
            "parent_version_id": parent_version_id,
            "candidate_version_id": candidate_version_id,
            "source_archive": {"size": expected_size, "sha256": expected_sha256},
            "file_count": len(records), "expanded_size": total, "files": records,
        }
        report = Path(report_path)
        report.parent.mkdir(parents=True, exist_ok=True)
        _write_json(report, manifest)
        return manifest
    except Exception as exc:
        # 当前半文件在打开后立即登记；清理错误作为补充，不覆盖复制错误。
        for path in reversed(created):
            try:
                if staging not in path.resolve().parents:
                    raise SourceCloneError("清理路径逃逸候选目录。")
                path.unlink(missing_ok=True)
            except Exception as cleanup:
                exc.add_note(f"克隆文件清理失败：{cleanup}")
        try:
            directories = sorted(
                (item for item in staging.rglob("*") if item.is_dir()),
                key=lambda item: len(item.parts), reverse=True,
            )
        except Exception as cleanup:
            exc.add_note(f"克隆清理目录遍历失败：{cleanup}")
            directories = []
        for directory in directories:
            try:
                if staging not in directory.resolve().parents or directory.is_symlink():
                    raise SourceCloneError("清理目录逃逸候选目录。")
                directory.rmdir()
            except Exception as cleanup:
                exc.add_note(f"克隆目录清理失败：{cleanup}")
        raise
