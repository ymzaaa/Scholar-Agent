# -*- coding: utf-8 -*-
"""可信反馈来源、私有源码克隆和已确认目标的统一执行。"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from infrastructure.stage1_adapter import clone_parent_latex_source
from persistence.store import AgentRun, Generation, ProjectStore
from generation.versions import (
    freeze_confirmed_snapshot, load_parent_snapshot, validate_trusted_generation,
)


##### 异常与产物校验板块 #####


class FeedbackContractError(RuntimeError):
    """反馈父版本、追踪目标或源码包不满足可信修订契约。"""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_artifact(
    generation: Generation, *, kind: str, name: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    matches = [
        item for item in generation.artifact_manifest.get("artifacts", [])
        if item.get("kind") == kind and (name is None or item.get("name") == name)
    ]
    if len(matches) != 1:
        raise FeedbackContractError(f"父版本缺少唯一的 {name or kind} 产物。")
    item = matches[0]
    if item.get("root") not in {"output", "report"}:
        raise FeedbackContractError("父版本产物根目录无效。")
    root_value = generation.output_dir if item.get("root") == "output" else generation.report_dir
    if not root_value:
        raise FeedbackContractError("父版本产物根目录缺失。")
    root = Path(root_value).resolve()
    path = (root / str(item.get("relative_path", ""))).resolve()
    if root not in path.parents or not path.is_file():
        raise FeedbackContractError("父版本产物路径无效。")
    if kind == "latex_source":
        # 源码包整体大小与哈希由克隆入口唯一校验，后端只定位登记路径。
        return path, item
    expected_size = int(item.get("size", -1))
    expected_hash = str(item.get("sha256") or "")
    if expected_size != path.stat().st_size or not expected_hash:
        raise FeedbackContractError("父版本产物大小或哈希清单不完整。")
    if _sha256_file(path) != expected_hash:
        raise FeedbackContractError("父版本产物哈希校验失败。")
    return path, item




##### 工作记忆与候选建立板块 #####

def _cleanup_unregistered_feedback(store, workspace) -> None:
    """仅清理本次已发布但尚未登记的私有反馈目录，永久版本不走此入口。"""
    if store.get_generation(workspace.generation_id) is not None:
        return
    workspace.abort()
    final = workspace.final_dir.resolve()
    project = workspace.manager.get_project(workspace.project.project_id)
    expected = project.generations_dir.resolve() / workspace.generation_id
    if not final.exists():
        return
    if final != expected or project.generations_dir.resolve() not in final.parents:
        raise ValueError("反馈清理目录不属于本次私有候选。")
    marker = json.loads((final / ".scholar-generation.json").read_text(encoding="utf-8"))
    if (marker.get("status") != "published" or marker.get("project_id") != project.project_id
        or marker.get("generation_id") != workspace.generation_id):
        raise ValueError("反馈私有目录所有权已经变化，停止清理。")
    shutil.rmtree(final)


def discard_private_feedback(store: ProjectStore, session) -> None:
    """会话事务已放弃后清理当前未登记副本；失败可按同一所有权证据重试。"""
    from infrastructure.stage1_adapter import _get_workspace_manager
    from pipeline.workspace import GenerationWorkspace
    from agents.quality_repair.action_journal import FileActionJournal

    if session.status != 'discarded':
        raise ValueError('只有已放弃会话可以清理私有反馈副本。')
    run = store.get_agent_run(session.state['current_run_id']) if session.state.get('current_run_id') else None
    if run is None or run.candidate_generation_id:
        return
    if run.project_id != session.project_id or run.state.get('session_id') != session.session_id:
        raise ValueError('待清理运行与已放弃会话归属不一致。')
    identifier = run.state.get('private_generation_id')
    if identifier:
        if str(uuid.UUID(identifier)) != identifier or store.get_generation(identifier) is not None:
            raise ValueError('待清理副本不是有效的未登记反馈候选。')
        manager = _get_workspace_manager()
        project = manager.get_project(session.project_id)
        stored = store.get(session.project_id)
        if stored is None or Path(stored.workspace_dir).resolve() != project.project_dir.resolve():
            raise ValueError('待清理工作区与项目归属不一致。')
        workspace = GenerationWorkspace(manager, project, identifier,
            project.generations_dir / ('.staging-' + identifier), project.generations_dir / identifier)
        if workspace.staging_dir.exists():
            manager._verify_generation_staging(workspace)
            FileActionJournal(workspace.staging_dir, project.reports_dir / 'agent-runs', run.run_id,
                              generation_id=identifier).rollback_all()
            workspace.abort()
    if run.status in {'running', 'waiting_user', 'validating'}:
        state = {**run.state, 'stop_reason': 'user_stopped', 'detail': '用户已放弃本次修订。'}
        if 'agent' in state:
            state['agent'] = {**state['agent'], 'status': 'stopped', 'stop_reason': 'user_stopped',
                              'detail': state['detail'], 'pending_action': None}
        store.update_agent_run(run.run_id, expected_revision=run.state_revision,
                               status='stopped', state=state, candidate_generation_id=None)




def execute_confirmed_feedback(store: ProjectStore, run_id: str, *, model, resume=False) -> AgentRun:
    """已确认目标共用统一图；私有路径只保存在运行中，完整交付后才登记候选。"""
    from copy import deepcopy
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command
    from revision.feedback_goals import _context, _persist_goal_state, load_feedback_evidence
    from infrastructure.stage1_adapter import _ensure_stage1_on_path, _get_workspace_manager
    _ensure_stage1_on_path()
    from agents.quality_repair.models import AgentState
    from pipeline.revision_batch import run_confirmed_feedback_candidate
    from pipeline.workspace import GenerationWorkspace
    from constraints.repository import inherited_for_session

    latest = store.get_agent_run(run_id)
    if latest is None:
        raise ValueError('反馈运行不存在。')
    latest, session = _context(store, run_id, latest.state_revision)
    if (latest.state.get('normalization_status') != 'completed' or not latest.state.get('goals')
        or any(goal.get('confirmed') is not True for goal in latest.state['goals'])
        or latest.status not in ({'waiting_user'} if resume else {'running'})):
        raise ValueError('反馈执行必须使用本批已确认目标及有效运行状态。')
    project = store.get(latest.project_id)
    parent = store.get_generation(latest.parent_generation_id)
    validate_trusted_generation(parent, project)
    inherited = inherited_for_session(store, latest, session)
    inherited_by_id = {item['constraint_id']: item for item in inherited}
    replaced = set()
    for goal in latest.state['goals']:
        for identifier in goal.get('supersedes_constraint_ids', []):
            if (identifier not in inherited_by_id
                or inherited_by_id[identifier].get('scope_ref') not in goal['target_unit_ids']):
                raise ValueError('已确认替代的保护与当前目标不一致。')
            replaced.add(identifier)
    inherited = [item for item in inherited if item['constraint_id'] not in replaced]
    evidence = load_feedback_evidence(store, run_id, expected_revision=latest.state_revision)
    snapshot = load_parent_snapshot(parent, project)
    manager = _get_workspace_manager()
    owned_project = manager.get_project(project.project_id)
    if owned_project.project_dir.resolve() != Path(project.workspace_dir).resolve():
        raise ValueError('反馈项目工作区与数据库归属不一致。')
    workspace = None
    try:
        if resume:
            identifier = str(latest.state.get('private_generation_id', ''))
            if str(uuid.UUID(identifier)) != identifier:
                raise ValueError('私有反馈候选编号无效。')
            state = AgentState.from_dict(latest.state['agent'])
            state.authorization = deepcopy(latest.state.get('authorization', {}))
            workspace = GenerationWorkspace(manager, owned_project, identifier,
                owned_project.generations_dir / f'.staging-{identifier}', owned_project.generations_dir / identifier)
            manager._verify_generation_staging(workspace)
            snapshot_path = latest.state['confirmed_snapshot_path']
            snapshot_hash = latest.state['confirmed_snapshot_sha256']
            expected_snapshot = owned_project.reports_dir / 'version-inputs' / identifier / 'confirmed_structure.json'
            if Path(snapshot_path).resolve() != expected_snapshot.resolve():
                raise ValueError('反馈冻结快照不属于本次私有候选。')
            frozen = expected_snapshot.read_bytes()
            if hashlib.sha256(frozen).hexdigest() != snapshot_hash or json.loads(frozen) != snapshot:
                raise ValueError('反馈冻结快照已经变化，不能恢复。')
        else:
            if latest.state.get('private_generation_id') or latest.state.get('agent'):
                raise ValueError('本批已经开始执行，请恢复原运行，不能创建另一候选。')
            identifier = str(uuid.uuid4())
            snapshot_path, snapshot_hash = freeze_confirmed_snapshot(project, identifier, snapshot,
                structure_revision=parent.structure_revision)
            latest = _persist_goal_state(store, latest, status='running', state={**latest.state,
                'private_generation_id': identifier, 'confirmed_snapshot_path': snapshot_path,
                'confirmed_snapshot_sha256': snapshot_hash})
            workspace = manager.begin_generation(project.project_id, identifier,
                version_context={'parent_version_id': parent.generation_id, 'change_origin': 'user_feedback'})
            archive, item = _verified_artifact(parent, kind='latex_source')
            clone_parent_latex_source(archive_path=archive, staging_dir=workspace.staging_dir,
                expected_size=int(item['size']), expected_sha256=item['sha256'],
                report_path=owned_project.reports_dir / 'agent-runs' / run_id / 'clone_manifest.json',
                parent_version_id=parent.generation_id, candidate_version_id=identifier)
            trace, _item = _verified_artifact(parent, kind='report', name='render_trace.json')
            (workspace.staging_dir / 'reports').mkdir(exist_ok=True)
            shutil.copyfile(trace, workspace.staging_dir / 'reports/render_trace.json')
            state = AgentState(run_id=run_id, project_id=project.project_id, mode='user_feedback',
                template_id=project.template_id, parent_generation_id=parent.generation_id,
                parent_source_sha256=parent.source_sha256, goals=deepcopy(latest.state['goals']),
                authorization=deepcopy(latest.state.get('authorization', {})))

        def persist(value):
            nonlocal latest
            latest = _persist_goal_state(store, latest, status=value['status'],
                state={**latest.state, 'agent': value, 'detail': value['detail']})

        def parent_identity():
            try:
                _context(store, run_id, latest.state_revision)
            except ValueError:
                return None, ''
            return parent.generation_id, parent.source_sha256

        checkpoint = owned_project.reports_dir / 'agent-checkpoints.sqlite'
        with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
            result = run_confirmed_feedback_candidate(workspace=workspace, state=state,
                evidence=evidence, snapshot=snapshot, structure_revision=parent.structure_revision,
                model=model, checkpointer=saver, parent_identity=parent_identity, on_state=persist,
                resume=Command(resume={'continue': True}) if resume else None,
                constraints=inherited)
        if not result['gate_snapshot']['published']:
            return latest
        store.register_trusted_feedback(run_id=run_id,
            identity=dict(generation_id=identifier, project_id=project.project_id,
                structure_revision=parent.structure_revision, source_sha256=parent.source_sha256,
                parent_generation_id=parent.generation_id, change_origin='user_feedback', created_by='agent',
                active_constraints=parent.active_constraints, confirmed_snapshot_path=snapshot_path,
                confirmed_snapshot_sha256=snapshot_hash),
            delivery={key: result[key] for key in ('quality_status', 'output_dir', 'report_dir',
                                                  'artifact_manifest', 'gate_snapshot')} | {
                'status': 'success' if result['quality_status'] == 'passed' else 'degraded',
                'detail': '用户确认的反馈目标已执行并通过完整终验。'})
        return store.get_agent_run(run_id)
    except Exception as exc:
        if workspace is not None:
            try:
                _cleanup_unregistered_feedback(store, workspace)
            except Exception as cleanup:
                exc.add_note(f'私有反馈副本清理失败：{type(cleanup).__name__}')
        raise
