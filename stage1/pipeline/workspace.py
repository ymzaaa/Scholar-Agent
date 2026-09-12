# -*- coding: utf-8 -*-
"""系统所有的项目工作区、generation 与 staging 生命周期。"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


##### 常量与异常板块 #####


WORKSPACE_MARKER = ".scholar-workspace.json"
PROJECT_MARKER = ".scholar-project.json"
GENERATION_MARKER = ".scholar-generation.json"
WORKSPACE_VERSION = "1.0.0"


class WorkspaceSafetyError(RuntimeError):
    """工作区所有权、目录状态或路径边界不满足安全要求。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_id(value: str, label: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise WorkspaceSafetyError(f"{label} 不是有效 UUID：{value!r}") from exc
    return str(parsed)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    # Windows 传统路径上限较短，generation 目录已包含两级 UUID；
    # 临时文件不重复拼接完整标记名，仍在同目录内完成原子替换。
    temporary = path.with_name(f".tmp-{uuid.uuid4().hex[:12]}")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as cleanup:
            exc.add_note(f"临时 JSON 清理失败：{cleanup}")
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceSafetyError(f"工作区标记不可读：{path}") from exc


##### 工作区数据模型板块 #####


@dataclass(frozen=True, slots=True)
class ProjectWorkspace:
    project_id: str
    project_dir: Path
    inputs_dir: Path
    generations_dir: Path
    reports_dir: Path


@dataclass(slots=True)
class GenerationWorkspace:
    manager: "WorkspaceManager"
    project: ProjectWorkspace
    generation_id: str
    staging_dir: Path
    final_dir: Path
    _published: bool = False

    def __enter__(self) -> "GenerationWorkspace":
        return self

    def publish(self) -> Path:
        """把完整 staging 原子发布为只读语义上的最终 generation。"""
        if self._published:
            return self.final_dir
        self.manager._verify_generation_staging(self)
        if self.final_dir.exists():
            raise WorkspaceSafetyError(f"generation 已存在，禁止覆盖：{self.final_dir}")
        marker = _read_json(self.staging_dir / GENERATION_MARKER)
        published_marker = {**marker, "status": "published", "published_at": _utc_now()}
        _write_json(self.staging_dir / GENERATION_MARKER, published_marker)
        try:
            self.staging_dir.replace(self.final_dir)
        except Exception as exc:
            try:
                _write_json(self.staging_dir / GENERATION_MARKER, marker)
            except Exception as restore:
                exc.add_note(f"恢复 staging 标记失败：{restore}")
            raise
        self._published = True
        return self.final_dir

    def abort(self) -> None:
        """只清理本对象创建且带所有权标记的 staging。"""
        if self._published or not self.staging_dir.exists():
            return
        self.manager._verify_generation_staging(self)
        shutil.rmtree(self.staging_dir)

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if not self._published:
            try:
                self.abort()
            except Exception as cleanup:
                if exc is None:
                    raise
                exc.add_note(f"staging 清理失败：{cleanup}")
        return False


##### 工作区管理板块 #####


class WorkspaceManager:
    """创建项目和版本；不提供删除已发布 generation 的通用接口。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self._initialize_root()
        self.projects_dir = self.root / "projects"
        self.projects_dir.mkdir(exist_ok=True)

    def _initialize_root(self) -> None:
        if self.root.exists() and not self.root.is_dir():
            raise WorkspaceSafetyError(f"工作区根不是目录：{self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        marker_path = self.root / WORKSPACE_MARKER
        entries = [entry for entry in self.root.iterdir() if entry.name != WORKSPACE_MARKER]
        if marker_path.exists():
            marker = _read_json(marker_path)
            if marker.get("workspace_version") != WORKSPACE_VERSION:
                raise WorkspaceSafetyError("工作区版本不兼容。")
            return
        if entries:
            raise WorkspaceSafetyError(
                f"拒绝接管没有所有权标记的非空目录：{self.root}"
            )
        _write_json(
            marker_path,
            {"workspace_version": WORKSPACE_VERSION, "created_at": _utc_now()},
        )

    def create_project(
        self,
        *,
        project_id: str | None = None,
        source_paths: dict[str, str] | None = None,
    ) -> ProjectWorkspace:
        identifier = _validate_id(project_id or str(uuid.uuid4()), "project_id")
        project_dir = self.projects_dir / identifier
        project_dir.mkdir(parents=False, exist_ok=False)
        project = ProjectWorkspace(
            project_id=identifier,
            project_dir=project_dir,
            inputs_dir=project_dir / "inputs",
            generations_dir=project_dir / "generations",
            reports_dir=project_dir / "reports",
        )
        try:
            for directory in (project.inputs_dir, project.generations_dir, project.reports_dir):
                directory.mkdir()
            sources = {
                key: str(Path(value).expanduser().resolve())
                for key, value in (source_paths or {}).items()
                if value
            }
            _write_json(
                project_dir / PROJECT_MARKER,
                {
                    "workspace_version": WORKSPACE_VERSION,
                    "project_id": identifier,
                    "created_at": _utc_now(),
                    "source_paths": sources,
                },
            )
        except Exception as exc:
            self._cleanup_created_directory(project_dir, self.projects_dir, exc)
            raise
        return project

    def get_project(self, project_id: str) -> ProjectWorkspace:
        identifier = _validate_id(project_id, "project_id")
        project_dir = (self.projects_dir / identifier).resolve()
        if self.projects_dir.resolve() not in project_dir.parents:
            raise WorkspaceSafetyError("project 路径逃逸工作区。")
        marker_path = project_dir / PROJECT_MARKER
        if not project_dir.is_dir() or not marker_path.is_file():
            raise WorkspaceSafetyError(f"project 不存在或无所有权标记：{identifier}")
        marker = _read_json(marker_path)
        if marker.get("project_id") != identifier:
            raise WorkspaceSafetyError("project 所有权标记不匹配。")
        return ProjectWorkspace(
            project_id=identifier,
            project_dir=project_dir,
            inputs_dir=project_dir / "inputs",
            generations_dir=project_dir / "generations",
            reports_dir=project_dir / "reports",
        )

    def begin_generation(
        self, project_id: str, generation_id: str | None = None,
        *, version_context: dict[str, Any] | None = None,
    ) -> GenerationWorkspace:
        project = self.get_project(project_id)
        identifier = _validate_id(generation_id or str(uuid.uuid4()), "generation_id")
        staging = project.generations_dir / f".staging-{identifier}"
        final = project.generations_dir / identifier
        if staging.exists() or final.exists():
            raise WorkspaceSafetyError(f"generation 已存在：{identifier}")
        staging.mkdir()
        try:
            _write_json(
                staging / GENERATION_MARKER,
                {
                    "workspace_version": WORKSPACE_VERSION,
                    "project_id": project.project_id,
                    "generation_id": identifier,
                    "status": "staging",
                    "created_at": _utc_now(),
                    "version": version_context or {},
                },
            )
        except Exception as exc:
            self._cleanup_created_directory(staging, project.generations_dir, exc)
            raise
        return GenerationWorkspace(self, project, identifier, staging, final)

    def _verify_generation_staging(self, generation: GenerationWorkspace) -> None:
        expected_parent = generation.project.generations_dir.resolve()
        resolved = generation.staging_dir.resolve()
        if resolved.parent != expected_parent or not resolved.name.startswith(".staging-"):
            raise WorkspaceSafetyError("拒绝操作工作区之外或名称异常的 staging。")
        marker_path = resolved / GENERATION_MARKER
        if not marker_path.is_file():
            raise WorkspaceSafetyError("staging 缺少系统所有权标记。")
        marker = _read_json(marker_path)
        if (
            marker.get("project_id") != generation.project.project_id
            or marker.get("generation_id") != generation.generation_id
            or marker.get("status") != "staging"
        ):
            raise WorkspaceSafetyError("staging 所有权标记不匹配。")

    def _cleanup_created_directory(self, path: Path, parent: Path, exc: Exception) -> None:
        """只用于本次 mkdir 成功后尚未完成标记的半成品，不接管已有目录。"""
        try:
            resolved = path.resolve()
            if resolved.parent != parent.resolve() or self.root not in resolved.parents or path.is_symlink():
                raise WorkspaceSafetyError("拒绝清理归属不明的创建半成品。")
            shutil.rmtree(resolved)
        except Exception as cleanup:
            exc.add_note(f"创建半成品清理失败：{cleanup}")


##### 渲染目录板块 #####


def prepare_render_directory(output_dir: str | Path) -> Path:
    """只允许新目录、空目录或系统 staging；永不删除已有内容。"""
    output = Path(output_dir).expanduser().resolve()
    if output.exists() and not output.is_dir():
        raise WorkspaceSafetyError(f"渲染目标不是目录：{output}")
    output.mkdir(parents=True, exist_ok=True)
    entries = list(output.iterdir())
    allowed = {GENERATION_MARKER}
    unexpected = [entry.name for entry in entries if entry.name not in allowed]
    if unexpected:
        raise WorkspaceSafetyError(
            f"渲染目标必须为空或为系统 staging，发现已有内容：{unexpected[:10]}"
        )
    if GENERATION_MARKER in {entry.name for entry in entries}:
        marker = _read_json(output / GENERATION_MARKER)
        if marker.get("status") != "staging":
            raise WorkspaceSafetyError("generation 标记不是 staging 状态。")
    return output


def default_workspace_root() -> Path:
    return Path(__file__).resolve().parents[2] / "workspace"
