# -*- coding: utf-8 -*-
"""P2：使用已注册模板把最小 Word 内容生成可审计首版 PDF。"""

from __future__ import annotations

import hashlib
import json
import copy
from dataclasses import asdict
from pathlib import Path
from typing import Any

from llm.policy import (
    HEADING_TRANSLATION_TASK,
    LLMAuthorization,
    LLMTaskResult,
    STATUS_SKIPPED_NOT_NEEDED,
)
from pipeline.chapter_renderer import render_chapter_files
from pipeline.confirmed_structure import apply_confirmed_structure, build_structure_snapshot, finalize_structure_snapshot
from pipeline.compiler import compile_project
from pipeline.content_fidelity_gate import build_content_fidelity_report
from pipeline.metadata_resolution import MetadataResolution, resolve_minimal_metadata
from pipeline.results import RenderResult
from pipeline.source_package import package_latex_source
from template_registry.models import TemplateManifestError
from pipeline.text_processing import escape_text
from pipeline.translate import translate_headings_for_toc


##### 模板准备板块 #####


def _prepare_template(root: Path, resolved: Any, metadata: MetadataResolution) -> str:
    adapter = resolved.adapter
    values = {key: escape_text(value) for key, value in metadata.values.items()}
    adapter.prepare_generation_staging(root, values)
    return adapter.content_directory


##### 源码包板块 #####


def _package_rebuildable_source(root: Path) -> dict[str, Any]:
    archive, records = package_latex_source(root)
    return {
        "path": archive.name, "file_count": len(records),
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "includes_sty": any(item.endswith(".sty") for item in records),
        "includes_eps": any(item.endswith(".eps") for item in records),
    }


def _example_content_absent(
    output: Path,
    template_adapter,
    chapter_files: list[str],
    bibliography_mode: str = "none",
) -> bool:
    return template_adapter.validate_generated_staging(
        output, chapter_files, bibliography_mode,
    )


##### 生成与门禁板块 #####


def _expected_role_counts(extracted: Any, recognized: Any) -> dict[str, int]:
    """按已确认的章节范围推导逻辑角色，不依赖某个测试案例的固定数量。"""
    counts: dict[str, int] = {}
    units = {
        item["unit_id"]: item
        for item in getattr(extracted, "content_units", [])
    }
    bound_object_ids = {
        object_id for item in recognized.review.get("object_bindings", [])
        for object_id in item.get("object_unit_ids", [])
    }
    confirmed_headings = {item["unit_id"]: item for item in recognized.review["heading_candidates"]}
    bindings = {item["caption_unit_id"]: item for item in recognized.review["object_bindings"]}
    for chapter in recognized.review["chapters"]:
        chapter_written = False
        start, end = chapter["range"]
        for index in range(start, min(end, len(extracted.paragraphs))):
            paragraph = extracted.paragraphs[index]
            text = paragraph.get("text", "").strip()
            inline_tokens = units.get(paragraph.get("unit_id"), {}).get(
                "payload", {}
            ).get("inline_tokens", [])
            has_semantic_content = any(
                token.get("kind") in {
                    "formula", "footnote", "field", "hyperlink", "text_box"
                }
                for token in inline_tokens
            )
            if not text and not has_semantic_content:
                continue
            confirmed = confirmed_headings.get(paragraph.get("unit_id"))
            heading = (
                (confirmed["level"], confirmed.get("number"), confirmed["title"])
                if confirmed and confirmed["level"] != "body" else None
            )
            if heading and heading[0] == "chapter" and not chapter_written:
                role = "chapter_heading"
                chapter_written = True
            elif heading and heading[0] in {"section", "subsection"}:
                role = f"{heading[0]}_heading"
            elif bindings.get(paragraph.get("unit_id"), {}).get("kind") == "figure":
                role = "figure_caption"
            elif bindings.get(paragraph.get("unit_id"), {}).get("kind") == "table":
                role = "table_caption"
            else:
                role = "body_paragraph"
            counts[role] = counts.get(role, 0) + 1
        start_block = extracted.paragraphs[start].get("block_index", -1)
        end_block = (
            extracted.paragraphs[end].get("block_index", float("inf"))
            if end < len(extracted.paragraphs) else float("inf")
        )
        for unit in units.values():
            block = unit.get("source", {}).get("block_index", -1)
            if (
                unit.get("unit_type") == "table"
                and unit.get("properties", {}).get("semantic_role") == "data"
                and unit.get("unit_id") not in bound_object_ids
                and start_block <= block < end_block
            ):
                counts["content_table"] = counts.get("content_table", 0) + 1
    return counts


def run_minimal_registered_generation(
    *,
    runner: Any,
    resolved_template: Any,
    repository_root: str | Path,
    docx_path: str | Path,
    output_directory: str | Path,
    case_id: str = "W01",
    translation_client: Any | None = None,
    report_filename: str = "p2-report.json",
    reference_source: str = "none",
    heading_decisions: dict[str, str] | None = None,
) -> dict[str, Any]:
    output = Path(output_directory).resolve()
    if output.exists():
        raise TemplateManifestError("P2 输出目录必须尚未创建。")
    extracted = runner.extract(str(docx_path))
    recognized = runner.recognize(extracted)
    from content_extraction.formula_validation import ensure_formula_preflight

    ensure_formula_preflight(recognized.review.get("formula_review", {}))
    from content_extraction.citations import apply_citation_overrides

    apply_citation_overrides(recognized.review.get("citation_review", {}), [])
    source_sha256 = extracted.extraction_report["metadata"]["source_sha256"]
    snapshot = build_structure_snapshot(extracted, recognized, source_sha256)
    for item in snapshot["headings"]:
        if item["unit_id"] in (heading_decisions or {}):
            item.update(level=heading_decisions[item["unit_id"]], overridden=True, review_status="resolved")
    snapshot = finalize_structure_snapshot(snapshot)
    snapshot["revision"] = 1
    recognized = apply_confirmed_structure(extracted, recognized, snapshot, source_sha256=source_sha256, structure_revision=1)
    resolved_template.materialize(repository_root, output)
    metadata = resolve_minimal_metadata(
        extracted.paragraphs,
        required_fields=resolved_template.adapter.required_metadata_fields,
    )
    content_directory = _prepare_template(output, resolved_template, metadata)
    contents = output / content_directory
    contents.mkdir(parents=True, exist_ok=True)
    if reference_source == "none":
        bibliography_plan = {
            "mode": "none", "all_citations_resolved": True,
            "required_numbers": [], "unresolved_numbers": [],
            "number_to_key": {}, "render_number_to_key": {},
            "body_citation_unit_ids": [], "transaction_status": "not_applicable",
            "citation_review": recognized.review.get("citation_review", {}),
        }
        resolved_template.adapter.update_bibliography(
            output / resolved_template.adapter.entrypoint, "none",
        )
    elif reference_source == "word":
        from pipeline.bibliography import prepare_bibliography_output

        bibliography_plan = prepare_bibliography_output(
            paragraphs=extracted.paragraphs,
            content_units=extracted.content_units,
            bib_path=None,
            output_dir=output,
            template_adapter=resolved_template.adapter,
            explicit_mapping=None,
            source_mode="word",
            citation_review=recognized.review.get("citation_review"),
        )
    else:
        raise TemplateManifestError(
            f"最小双模板生成不支持参考文献模式：{reference_source}"
        )
    cite_map = bibliography_plan["render_number_to_key"]
    citation_decisions = {
        item["unit_id"]: item
        for item in bibliography_plan.get("citation_review", {}).get("decisions", [])
    }
    stats = {"figures": 0, "tables": 0, "equations": 0, "citations": 0}
    chapter_files, trace = render_chapter_files(
        chapters=recognized.review["chapters"],
        object_bindings=recognized.review.get("object_bindings", []),
        paragraphs=extracted.paragraphs,
        content_units=extracted.content_units, cite_map=cite_map, contents_dir=contents,
        stats=stats, source_schema_version=extracted.extraction_report["schema_version"],
        template_adapter=resolved_template.adapter,
        content_directory=content_directory,
        citation_decisions=citation_decisions,
        confirmed_headings={item["unit_id"]: item for item in recognized.review["heading_candidates"]},
    )
    trace["bibliography"] = bibliography_plan
    resolved_template.adapter.update_chapter_includes(
        output / resolved_template.manifest.compile_recipe.entrypoint, chapter_files
    )
    resolved_template.adapter.validate_chapter_includes(
        output / resolved_template.manifest.compile_recipe.entrypoint, chapter_files
    )
    if resolved_template.manifest.capabilities.get(
        "required_english_translation"
    ) == "supported":
        translation = translate_headings_for_toc(
            output,
            profile=resolved_template.adapter.format_contract(),
            template_adapter=resolved_template.adapter,
            heading_records=trace.get("headings", []),
            authorization=LLMAuthorization.for_tasks(
                [HEADING_TRANSLATION_TASK],
                external_processing_allowed=True,
                source="built_in_template_required_translation",
            ),
            client=translation_client,
        )
    else:
        translation = LLMTaskResult(
            task=HEADING_TRANSLATION_TASK,
            status=STATUS_SKIPPED_NOT_NEEDED,
            detail="当前模板未声明英文目录能力。",
        )
    rendered = RenderResult(str(output), stats, chapter_files, trace, template_adapter=resolved_template.adapter)
    fidelity = build_content_fidelity_report(extracted, recognized, rendered)
    role_counts: dict[str, int] = {}
    for record in trace["records"]:
        if record.get("status") == "rendered":
            role = record["role"]
            role_counts[role] = role_counts.get(role, 0) + 1
    expected_roles = _expected_role_counts(extracted, recognized)
    from pipeline.structural_gate import structural_fidelity_gate

    structural_checks = structural_fidelity_gate(
        extracted.word_structure, contents, confirmed_structure=recognized.review,
        main_tex_path=output / resolved_template.adapter.entrypoint,
        chapter_files=chapter_files, template_adapter=resolved_template.adapter,
    )
    structure_ok = role_counts == expected_roles
    formula_units = [
        item for item in extracted.content_units
        if item.get("unit_type") == "formula"
    ]
    formula_metric_names = (
        "formula_coverage", "omml_formula_coverage", "latex_formula_coverage",
        "formula_render_order", "display_formula_coverage",
    )
    formula_metrics = {
        name: fidelity["metrics"][name] for name in formula_metric_names
    }
    examples_absent = _example_content_absent(
        output, resolved_template.adapter, chapter_files, bibliography_plan["mode"],
    )
    source_package = _package_rebuildable_source(output)
    recipe = resolved_template.manifest.compile_recipe
    compiled = compile_project(
        output, recipe,
    )
    degradations = ["missing_metadata"] if metadata.missing_fields else []
    quality_status = "degraded" if degradations else "passed"
    report = {
        "schema_version": "1.0.0", "case_id": case_id,
        "template": {
            "template_id": resolved_template.manifest.template_id,
        },
        "metadata": {
            "values": metadata.values, "items": list(metadata.items),
            "missing_fields": list(metadata.missing_fields),
        },
        "content": {"role_counts": role_counts, "expected_roles": expected_roles},
        "formulas": {
            "inventory": [
                {
                    "unit_id": item["unit_id"], "latex": item.get("text", ""),
                    "source_syntax": item.get("payload", {}).get("source_syntax"),
                    "delimiter": item.get("payload", {}).get("delimiter"),
                    "display": bool(item.get("properties", {}).get("display")),
                    "status": item.get("status"),
                }
                for item in formula_units
            ],
            "metrics": formula_metrics,
            "all_passed": all(
                metric["status"] in {"pass", "not_applicable"}
                for metric in formula_metrics.values()
            ),
        },
        "references": {
            "mode": bibliography_plan["mode"],
            "transaction_status": bibliography_plan["transaction_status"],
            "required_numbers": bibliography_plan["required_numbers"],
            "unresolved_numbers": bibliography_plan["unresolved_numbers"],
            "number_to_key": bibliography_plan["number_to_key"],
            "citation_review": bibliography_plan.get("citation_review", {}),
        },
        "gates": {
            "content_fidelity": fidelity.get("all_passed"),
            "logical_structure": structure_ok,
            "structural_counts": structural_checks,
            "compile": compiled.success,
            "example_content_absent": examples_absent,
            "required_translation": not translation.blocking,
        },
        "degradations": degradations,
        "quality_status": quality_status,
        "published": bool(
            fidelity.get("all_passed") and structure_ok
            and examples_absent and not translation.blocking and compiled.success
        ),
        "source_package": source_package,
        "llm_tasks": [translation.to_dict()],
        "compile": {
            "success": compiled.success, "pdf_path": compiled.pdf_path,
            "errors": compiled.raw_error, "gates": compiled.gates,
            "processes": [asdict(item) for item in compiled.process_results],
        },
    }
    reports = output / "reports"
    reports.mkdir(exist_ok=True)
    if Path(report_filename).name != report_filename or not report_filename.endswith(".json"):
        raise TemplateManifestError("报告文件名必须是安全的 JSON 文件名。")
    (reports / report_filename).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report
