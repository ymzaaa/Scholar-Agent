# -*- coding: utf-8 -*-
"""持久化抽取、一次识别、确认结构渲染及编译的稳定库接口。"""

import json
import os
import shutil
import sys
from pathlib import Path

##### 依赖与路径板块 #####
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PIPELINE_DIR = Path(__file__).resolve().parent / 'pipeline'
sys.path.insert(0, str(_PIPELINE_DIR.parent))

from pipeline.results import (
    CheckResult,
    CompileResult,
    ExtractResult,
    RecognizeResult,
    RenderResult,
)
from pipeline.compiler import compile_project
from pipeline.workspace import prepare_render_directory
from adapters.ouc import OUCTemplateAdapter
from pipeline.text_processing import escape_cell, escape_text

##### 前置内容渲染板块 #####
def generate_cover(
    paragraphs, docx_tables, output_dir, template_adapter
):
    """提取 Word 封面元数据并交给适配器写入，禁止在流水线中重建模板布局。"""
    from pipeline.cover_metadata import resolve_cover_metadata

    adapter = template_adapter
    if not hasattr(adapter, "prepare_generation_staging"):
        raise TypeError("模板适配器未实现 prepare_generation_staging。")

    metadata = resolve_cover_metadata(paragraphs, docx_tables)
    escaped = {
        key: escape_text(item)
        for key, item in metadata.items()
    }
    adapter.prepare_generation_staging(Path(output_dir), escaped)


def generate_abstract(paragraphs, output_dir, template_adapter, lang='ch'):
    """Generate abstract .tex. Generic: auto-detects boundaries by marker text."""
    if lang == 'ch':
        marker = '摘    要'
        kw_prefix = '关键词：'
        env = 'abstract'; filename = 'abstract_ch.tex'
        prefix = '% 中文摘要\n\\pagenumbering{1}\n'
    else:
        marker = 'ABSTRACT'
        kw_prefix = 'Key Words: '
        env = 'enabstract'; filename = 'abstract_en.tex'
        prefix = '% 英文摘要\n'

    # Find range
    start = None
    for i, p in enumerate(paragraphs):
        if p['text'].strip() == marker:
            start = i + 1  # skip heading
            break
    if start is None:
        for i, p in enumerate(paragraphs):
            if marker in p['text']:
                start = i + 1
                break
    if start is None:
        return

    # Find end: look for next section marker or blank-line gap
    end_markers = ['ABSTRACT', '摘    要', '图 表 清 单', '目录', '注释表', '1 ']
    if lang == 'ch':
        end_markers = ['ABSTRACT', '图 表 清 单', '目录', '注释表']
    else:
        end_markers = ['图 表 清 单', '目录', '注释表', '1 ']

    end = start
    for j in range(start, min(start + 30, len(paragraphs))):
        text = paragraphs[j]['text'].strip()
        if text in end_markers:
            end = j
            break
    if end == start:
        end = min(start + 15, len(paragraphs))

    lines = [prefix, f'\\begin{{{env}}}']
    for idx in range(start, end):
        text = paragraphs[idx]['text'].strip()
        if not text:
            lines.append('')
            continue
        if text == marker:
            continue

        # Keywords line
        if text.startswith('关键词：') or text.startswith('Key Words'):
            processed = escape_text(text)
            for pfx in ['关键词：', 'Key Words：', 'Key Words']:
                if pfx in processed:
                    key_content = processed.split(pfx)[-1].strip()
                    actual_kw = kw_prefix
                    lines.append('')
                    lines.append(f'\\noindent\\textbf{{{actual_kw}}}{key_content}')
                    break
        else:
            processed = escape_text(text)
            if processed:
                lines.append(processed)
                lines.append('')

    # Fix English keywords format
    body = '\n'.join(lines)
    if lang == 'en':
        body = body.replace('Key Words：', 'Key Words: ')
    lines = body.split('\n')
    lines.append(f'\\end{{{env}}}')

    template_adapter.write_abstract(Path(output_dir), lang, '\n'.join(lines))


def generate_abbreviation(docx_tables, output_dir):
    """Generate abbreviation.tex from docx table[1] (generic: assumes table[1] is abbreviation)."""
    if len(docx_tables) < 2: return
    abbr = docx_tables[1]; rows = abbr['data']
    # Verify it looks like an abbreviation table
    if not rows or rows[0][0].strip() != '英文缩写':
        return
    lines = ['\\begin{abbreviation}',
             '\\begin{tabular}{p{2.8cm}p{6.2cm}p{5cm}}',
             '\\toprule[1.5pt]',
             '英文缩写 & 英文全称 & 中文全称 \\\\',
             '\\midrule[0.5pt]']
    for row in rows[1:]:
        if len(row) >= 3 and row[0].strip():
            lines.append(' & '.join(escape_cell(row[i]) for i in range(3)) + ' \\\\')
    lines.extend(['\\bottomrule[1.5pt]', '\\end{tabular}', '\\end{abbreviation}'])
    with open(os.path.join(output_dir, 'data', 'abbreviation.tex'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


##### 流水线接口板块 #####

class PipelineRunner:
    """双 OUC 确定性流水线；确认结构是渲染和门禁的共同输入。"""

    def __init__(self, template_adapter=None, resolved_template=None):
        """绑定已注册模板上下文；无上下文仅保留历史 OUC CLI 兼容。"""
        if resolved_template is not None:
            self.template_adapter = resolved_template.adapter
            self.template_manifest = resolved_template.manifest
        else:
            self.template_adapter = template_adapter or OUCTemplateAdapter()
            self.template_manifest = None
        self.compile_recipe = getattr(self.template_adapter, "compile_recipe", None)
        self.template_entrypoint = self.template_adapter.entrypoint
        self.content_directory = self.template_adapter.content_directory

    # ---- Stage 1: Extract ----
    def extract(self, docx_path: str) -> ExtractResult:
        """只读抽取 Word 有序内容；持久化由应用服务负责。"""
        from content_extraction.docx_extractor import extract_docx_ordered

        bundle = extract_docx_ordered(docx_path)
        return ExtractResult(
            paragraphs=bundle.paragraphs,
            tables=bundle.tables,
            content_units=bundle.units_as_dicts(),
            extraction_report=bundle.report(),
            media_assets=bundle.media_assets,
        )

    # 一次识别：正文区域、文档证据、候选与初步章节。
    def recognize(self, extract_result: ExtractResult) -> RecognizeResult:
        """在持久化抽取结果上一次识别结构；不写文件，也不构造重复报告。"""
        from pipeline.structure_recognizer import (
            recognize_regions, recognize_heading_candidates, chapters_from_headings,
            recognize_object_bindings,
        )
        from content_extraction.citations import (
            detect_word_reference_list, classify_numeric_citation_candidates,
        )
        from content_extraction.formula_validation import build_formula_review

        paragraphs = extract_result.paragraphs
        regions = recognize_regions(paragraphs, extract_result.content_units)
        headings = recognize_heading_candidates(paragraphs, regions=regions, content_units=extract_result.content_units)
        chapters = chapters_from_headings(headings, regions)
        start, end = regions["body_range"]
        first_block = paragraphs[start].get("block_index", -1) if start < len(paragraphs) else -1
        end_block = paragraphs[end].get("block_index", float("inf")) if end < len(paragraphs) else float("inf")
        excluded_blocks = {p.get("block_index") for p in paragraphs if p.get("generated_contents")}
        body_units = [
            unit for unit in extract_result.content_units
            if first_block <= unit.get("source", {}).get("block_index", -1) < end_block
            and unit.get("source", {}).get("block_index") not in excluded_blocks
        ]
        reference_region = detect_word_reference_list(paragraphs)
        return RecognizeResult(
            review={
                "chapters": chapters, "regions": regions,
                "heading_candidates": headings,
                "object_bindings": recognize_object_bindings(body_units),
                "reference_region": reference_region,
                "citation_review": classify_numeric_citation_candidates(
                    paragraphs, extract_result.content_units, reference_region,
                ),
                "formula_review": build_formula_review(extract_result.content_units),
            },
            internal={},
        )

    # ---- Stage 0+2+3: Render ----
    def render(self,
               extract_result: ExtractResult,
               recognize_result: RecognizeResult,
               template_dir: str,
               output_dir: str,
               bib_path: str | None,
               citation_mapping: dict[str, str] | None = None,
               reference_source: str = 'auto') -> RenderResult:
        """复制模板并消费已经确认的识别、对象和参考文献结果。"""
        from content_extraction.formula_validation import ensure_formula_preflight

        ensure_formula_preflight(recognize_result.review.get('formula_review', {}))
        from content_extraction.citations import apply_citation_overrides

        apply_citation_overrides(recognize_result.review.get('citation_review', {}), [])
        from pipeline.confirmed_structure import resolve_render_structure

        recognize_result = resolve_render_structure(extract_result, recognize_result)
        paragraphs = extract_result.paragraphs
        docx_tables = extract_result.tables

        # ---- Stage 0: Copy template ----
        # 输出必须是新目录、空目录或系统 staging；渲染器不再删除任何最终目录。
        output_dir = str(prepare_render_directory(output_dir))
        if self.template_manifest is not None:
            from template_registry.materialize import materialize_template

            materialize_template(self.template_manifest, _PROJECT_ROOT, output_dir)
        else:
            shutil.copytree(template_dir, output_dir, dirs_exist_ok=True)

        # ---- 模板 staging 与封面元数据 ----
        # 适配器可在副本中建立受控插槽；固定模板源始终保持只读。
        generate_cover(
            paragraphs, docx_tables, output_dir,
            template_adapter=self.template_adapter,
        )

        # ---- 参考文献数据源与编号映射 ----
        from pipeline.bibliography import prepare_bibliography_output

        bibliography_plan = prepare_bibliography_output(
            paragraphs=paragraphs,
            content_units=extract_result.content_units,
            bib_path=bib_path,
            output_dir=output_dir,
            template_adapter=self.template_adapter,
            explicit_mapping=(
                citation_mapping
                if citation_mapping is not None
                else recognize_result.internal.get('citation_mapping')
            ),
            source_mode=(
                reference_source
                if reference_source != 'auto'
                else recognize_result.internal.get('reference_source', 'auto')
            ),
            citation_review=recognize_result.review.get('citation_review'),
        )

        # ---- Stage 2: Abstracts + Abbreviation ----
        generate_abstract(paragraphs, output_dir, self.template_adapter, 'ch')
        generate_abstract(paragraphs, output_dir, self.template_adapter, 'en')
        generate_abbreviation(docx_tables, output_dir)

        # ---- Word 内嵌图片资产 ----
        from pipeline.object_renderer import materialize_word_assets

        asset_manifest = materialize_word_assets(
            extract_result.content_units, extract_result.media_assets, output_dir
        )

        cite_map = bibliography_plan['render_number_to_key']

        # ---- Chapter rendering: consume pre-computed recognition data ----
        chapters = recognize_result.review['chapters']
        content_directory = self.content_directory
        contents_dir = os.path.join(output_dir, content_directory)
        os.makedirs(contents_dir, exist_ok=True)

        stats = {'figures': 0, 'tables': 0, 'equations': 0, 'citations': 0}

        from pipeline.chapter_renderer import render_chapter_files

        chapter_files, render_trace = render_chapter_files(
            chapters=chapters,
            object_bindings=recognize_result.review.get('object_bindings', []),
            paragraphs=paragraphs,
            content_units=extract_result.content_units,
            cite_map=cite_map,
            contents_dir=contents_dir,
            stats=stats,
            source_schema_version=extract_result.extraction_report.get('schema_version', ''),
            asset_manifest=asset_manifest,
            confirmed_headings={item['unit_id']: item for item in recognize_result.review['heading_candidates']},
            template_adapter=self.template_adapter,
            content_directory=content_directory,
            citation_decisions={
                item['unit_id']: item
                for item in bibliography_plan.get('citation_review', {}).get('decisions', [])
            },
        )
        render_trace['bibliography'] = bibliography_plan
        self.template_adapter.update_chapter_includes(
            Path(output_dir) / self.template_entrypoint, chapter_files
        )

        return RenderResult(
            template_adapter=self.template_adapter,
            output_dir=output_dir,
            stats=stats,
            chapter_files=chapter_files,
            render_trace=render_trace,
        )

    # ---- Stage 6: Compile ----
    def compile(self, tex_path: str, timeout_seconds: int = 120) -> CompileResult:
        """执行确定性编译，并由进程、PDF、日志和引用门禁统一判定成功。"""
        output_dir = os.path.dirname(tex_path) if tex_path.endswith('.tex') else tex_path
        recipe = self.compile_recipe
        if recipe is None:
            raise RuntimeError("编译前必须绑定已注册模板及其 CompileRecipe。")
        return compile_project(
            output_dir, recipe,
            timeout_seconds=timeout_seconds,
        )

    # ---- Stage 7: Format checker ----
    def check_format(self, tex_dir: str, rules_path: str | None = None) -> CheckResult:
        """按通用规则和当前模板适配器画像执行分层格式检查。"""
        if rules_path is None:
            rules_path = os.path.join(
                _PROJECT_ROOT, 'stage1', 'rules', 'common-format-rules.json'
            )

        from pipeline.checker.format_checker import FormatChecker
        checker = FormatChecker(
            rules_path,
            tex_dir,
            self.template_adapter.format_rule_profile(),
        )
        results = checker.run_all()
        matrix = checker.coverage_matrix()
        coverage_path = Path(tex_dir) / 'reports' / 'format_coverage_matrix.json'
        coverage_path.parent.mkdir(parents=True, exist_ok=True)
        coverage_path.write_text(
            json.dumps(matrix, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        return CheckResult(
            all_passed=matrix['all_passed'],
            fails=matrix['blockers'],
            results=results,
            quality_status=matrix['quality_status'],
            publish_allowed=matrix['publish_allowed'],
            degradations=matrix['degradations'],
            counts=matrix['counts'],
            template_profile=matrix['template'],
        )
