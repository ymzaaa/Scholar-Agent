# -*- coding: utf-8 -*-
"""从冻结 generation 记录执行首次生成，验证交付后登记可信状态。"""

from __future__ import annotations

from pathlib import Path
import json
import traceback

from infrastructure.stage1_adapter import run_project_generation
from persistence.store import ProjectStore
from infrastructure.template_catalog import resolve_template_binding
from pipeline.generation_service import _artifact_manifest
from pipeline.workspace import _write_json, GENERATION_MARKER


##### 失败收敛板块 #####


def _record_failure(store, initial, project, exc):
    """即使常规终态更新失败，也保留内部诊断并使用最小事务收敛版本。"""
    manifest = {"version": "1.0.0", "artifacts": []}
    report_dir = None
    if project is not None:
        report_dir = Path(project.workspace_dir).resolve() / "reports" / initial.generation_id
        try:
            if Path(project.workspace_dir).resolve() not in report_dir.resolve().parents:
                raise ValueError("生成报告路径越界。")
            report_dir.mkdir(parents=True, exist_ok=True)
            output = Path(project.workspace_dir).resolve() / "generations" / initial.generation_id
            published = False
            if output.is_dir():
                try:
                    marker = json.loads((output / GENERATION_MARKER).read_text(encoding="utf-8"))
                    published = (isinstance(marker, dict) and marker.get("status") == "published"
                        and marker.get("project_id") == project.project_id
                        and marker.get("generation_id") == initial.generation_id)
                except (OSError, UnicodeError, ValueError):
                    pass
            _write_json(report_dir / "delivery-error.json", {
                "published": published, "registered": False, "quality_status": "internal_error",
                "failure_stage": "generation", "code": "generation-registration-error",
                "error": str(exc), "traceback": traceback.format_exc(),
            })
            manifest = _artifact_manifest(None, report_dir)
        except Exception as report_error:
            exc.add_note(f"内部错误报告写入失败：{report_error}")
    # 不通过被本次异常中断的普通更新路径重试完整结果；完整正式目录保留但不授信。
    store.fail_generation(initial.generation_id, report_dir=str(report_dir) if report_dir else None,
                          artifact_manifest=manifest, detail="生成或交付登记异常，未登记为可信版本。")


##### 后台执行板块 #####


def _persist_initial_agent(store, generation, project, state):
    """只为实际启动的首版 Agent 保存状态，关联预留任务不会提前授信。"""
    if (not isinstance(state, dict) or state.get('run_id') != generation.generation_id
        or state.get('project_id') != project.project_id or state.get('mode') != 'initial_generation'
        or state.get('template_id') != project.template_id
        or state.get('parent_source_sha256') != generation.source_sha256
        or state.get('status') not in {'running', 'waiting_user', 'validating', 'ready', 'stopped', 'failed'}):
        raise ValueError('首版 Agent 状态与预留生成任务不一致。')
    current = store.get_generation(generation.generation_id)
    if (current is None or current.trusted or current.review_status != 'not_reviewable'
        or current.status not in {'queued', 'running'}):
        raise ValueError('首版 Agent 只能关联当前未可信的生成任务。')
    run = store.get_agent_run(state['run_id'])
    if run is None:
        return store.create_agent_run(run_id=state['run_id'], project_id=project.project_id,
            parent_generation_id=generation.parent_generation_id or '', mode='initial_generation',
            candidate_generation_id=generation.generation_id, status=state['status'], state={'agent': state})
    if (run.project_id != project.project_id or run.mode != 'initial_generation'
        or run.candidate_generation_id != generation.generation_id):
        raise ValueError('已有 Agent 运行不能改为其他生成任务。')
    if run.status == state['status'] and run.state.get('agent') == state:
        return run
    return store.update_agent_run(run.run_id, expected_revision=run.state_revision, status=state['status'],
        state={**run.state, 'agent': state}, candidate_generation_id=generation.generation_id)


def execute_generation(
    *, database_path: str, generation_id: str,
    external_processing_allowed: bool = False,
    allowed_llm_tasks: list[str] | None = None,
) -> dict:
    """只接收记录标识和本次授权，其余输入一律来自冻结数据库记录。"""
    store = ProjectStore(database_path)
    initial = store.get_generation(generation_id)
    if initial is None:
        raise KeyError(generation_id)
    if initial.status not in {"queued", "running"}:
        return {"generation_id": generation_id, "status": initial.status,
                "quality_status": initial.quality_status}
    project = None
    try:
        project = store.get(initial.project_id)
        if project is None or initial.source_sha256 != project.source_sha256:
            raise ValueError("生成任务项目或源文件哈希不一致。")
        from generation.versions import load_parent_snapshot
        load_parent_snapshot(initial, project)
        binding = resolve_template_binding(project.template_id)
        store.update_generation(generation_id, status="running", quality_status="pending",
                                detail="正在生成并执行发布门禁。")
        version_context = {
            "version_id": initial.generation_id, "parent_version_id": initial.parent_generation_id,
            "version_number": initial.version_number, "change_origin": initial.change_origin,
            "created_by": initial.created_by, "active_constraints": initial.active_constraints,
        }
        result = run_project_generation(
            project_id=project.project_id, generation_id=generation_id,
            bib_path=project.bib_path, citation_map_path=project.citation_map_path,
            reference_source=project.reference_source, source_sha256=initial.source_sha256,
            template_id=binding.template_id, structure_revision=initial.structure_revision,
            confirmed_snapshot_path=initial.confirmed_snapshot_path, version_context=version_context,
            external_processing_allowed=external_processing_allowed,
            allowed_llm_tasks=list(allowed_llm_tasks or []),
            on_agent_state=lambda state: _persist_initial_agent(store, initial, project, state),
        )
        if not isinstance(result, dict):
            raise ValueError("生成结果必须是结构化对象。")
        required = {"status", "quality_status", "output_dir", "report_dir", "artifact_manifest", "detail", "gate_snapshot"}
        if not required <= result.keys() or not isinstance(result["gate_snapshot"], dict):
            raise ValueError("生成结果契约不完整。")
        if result["status"] not in {"success", "degraded", "failed", "reverted"}:
            raise ValueError("生成结果包含未知终态。")
        gates = result["gate_snapshot"]
        statuses = gates.get("gate_statuses", {})
        gate_allowed = (isinstance(statuses, dict)
            and statuses.get("content-fidelity") in {"passed", "degraded"}
            and statuses.get("structure") == "passed" and statuses.get("compile") == "passed"
            and statuses.get("format") in {"passed", "degraded"})
        publishable = result["status"] in {"success", "degraded"} and gates.get("published") is True and gate_allowed
        if result["status"] in {"success", "degraded"} and not publishable:
            raise ValueError("生成成功状态与发布门禁状态不一致。")
        report_dir = Path(project.workspace_dir).resolve() / "reports" / generation_id
        if not result["report_dir"] or Path(result["report_dir"]).resolve() != report_dir:
            raise ValueError("生成结果报告目录不属于本 generation。")
        manifest = result["artifact_manifest"]
        if not isinstance(manifest, dict) or not isinstance(manifest.get("artifacts"), list):
            raise ValueError("生成产物清单无效。")
        if publishable:
            if not any(item.get("kind") == "report" and item.get("name") == "pipeline_log.json"
                       for item in manifest["artifacts"]):
                raise ValueError("首次生成缺少流水线审计报告。")
        else:
            manifest = {**manifest, "artifacts": [
                item for item in manifest["artifacts"] if item.get("kind") == "report"
            ]}
        final_status = result["status"] if publishable else "reverted" if initial.parent_generation_id else "failed"
        if publishable:
            generation = store.publish_trusted_generation(
                generation_id, status=final_status, quality_status=result["quality_status"],
                output_dir=result["output_dir"], report_dir=result["report_dir"],
                artifact_manifest=manifest, detail=result["detail"], gate_snapshot=gates)
        else:
            generation = store.update_generation(
                generation_id, status=final_status, quality_status=result["quality_status"],
                output_dir=None, report_dir=result["report_dir"], artifact_manifest=manifest,
                detail=result["detail"], trusted=False, review_status="not_reviewable",
                rollback_target_id=initial.parent_generation_id, gate_snapshot=gates)
        return {"generation_id": generation.generation_id, "status": generation.status,
                "quality_status": generation.quality_status}
    except Exception as exc:
        try:
            _record_failure(store, initial, project, exc)
        except Exception as persistence_error:
            exc.add_note(f"生成失败终态登记失败：{persistence_error}")
        try:
            run = store.get_agent_run(generation_id)
            if (run is not None and run.project_id == initial.project_id
                and run.mode == 'initial_generation' and run.candidate_generation_id == generation_id
                and run.status in {'running', 'validating', 'waiting_user'}):
                # 交付中断不虚构回滚成功，也不改写已完成 Agent 的真实终验结果。
                state = {**run.state.get('agent', {}), 'status': 'failed',
                    'stop_reason': 'initial_generation_interrupted',
                    'detail': '首次生成执行中断，未登记为可信版本。'}
                store.update_agent_run(run.run_id, expected_revision=run.state_revision,
                    status='failed', state={**run.state, 'agent': state},
                    candidate_generation_id=generation_id)
        except Exception as persistence_error:
            exc.add_note(f"首版 Agent 中断状态登记失败：{type(persistence_error).__name__}")
        raise
