# -*- coding: utf-8 -*-
"""模板必需语言插槽的确定性发现、校验与模型批处理协议。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from llm.policy import (
    CAPTION_TRANSLATION_TASK,
    HEADING_TRANSLATION_TASK,
    LLMTaskResult,
    STATUS_SKIPPED_NOT_NEEDED,
)


##### 插槽发现板块 #####


def _append_unique(entries: list[tuple[str, str]], kind: str, value: str) -> None:
    item = (kind, value.strip())
    if item[1] and item not in entries:
        entries.append(item)


def _collect_missing_language_inputs(
    output_dir: str | Path,
    template_adapter,
    heading_records: list[dict],
    *,
    category: str,
) -> tuple[list, list[tuple[str, str]]]:
    """只让声明了语言能力的适配器发现插槽。"""
    discover = getattr(template_adapter, "discover_language_slots", None)
    if discover is None:
        return [], []
    slots = [
        item for item in discover(output_dir, heading_records)
        if item.category == category
        and not _valid_english_text(item.source_text, item.current_text)
    ]
    entries: list[tuple[str, str]] = []
    for slot in slots:
        _append_unique(entries, slot.kind, slot.source_text)
    return slots, entries


def _input_hashes(entries: list[tuple[str, str]]) -> list[str]:
    return [
        hashlib.sha256(f"{kind}\0{text}".encode("utf-8")).hexdigest()
        for kind, text in entries
    ]


_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _valid_english_text(source: str, translated: str) -> bool:
    normalized = translated.strip()
    if not normalized or normalized == source.strip():
        return False
    visible = re.sub(r"\s+", "", normalized)
    return (
        bool(re.search(r"[A-Za-z]", visible))
        and len(_CJK_RE.findall(visible)) <= max(1, len(visible) // 5)
    )


##### 模型批处理协议板块 #####


_SYSTEM_PROMPT = (
    "You translate headings and captions from a Chinese academic thesis into English. "
    "Preserve abbreviations and proper names already present in the input. "
    "Do not add facts, explanations, domain assumptions, or information absent from the source. "
    "Return exactly the requested ID lines."
)


def _translate_all(
    client,
    authorization,
    entries: list[tuple[str, str]],
    *,
    task: str = HEADING_TRANSLATION_TASK,
) -> tuple[dict[tuple[str, str], str], Exception | None]:
    """已授权语言任务严格按批次和 ID 接收结果，非法返回不隐式重试。"""
    translations: dict[tuple[str, str], str] = {}
    batch_size = 15
    for start in range(0, len(entries), batch_size):
        batch = entries[start:start + batch_size]
        lines = [
            "Translate every item. Output exactly one 'ID=N: English' line per item, "
            "with no Markdown or additional text.",
            "",
        ]
        lines.extend(
            f"ID={start + offset} | TYPE={kind} | TEXT={text}"
            for offset, (kind, text) in enumerate(batch)
        )
        try:
            response = client.complete_text(
                _SYSTEM_PROMPT,
                "\n".join(lines),
                authorization=authorization,
                task=task,
            )
        except Exception as exc:
            return translations, exc
        if not isinstance(response, str) or not response.strip():
            return translations, ValueError("模型没有返回有效翻译文本。")
        parsed: dict[int, str] = {}
        expected = set(range(start, start + len(batch)))
        for line in response.splitlines():
            if not line.strip():
                continue
            match = re.fullmatch(r"\s*ID\s*=\s*(\d+)\s*:\s*(.+?)\s*", line)
            if not match:
                return translations, ValueError("模型返回了翻译协议之外的文字。")
            index = int(match.group(1))
            value = match.group(2).strip()
            if index in parsed:
                return translations, ValueError("模型返回了重复 ID。")
            if index not in expected:
                return translations, ValueError("模型返回了越界 ID。")
            parsed[index] = value
        if set(parsed) != expected:
            return translations, ValueError("模型返回 ID 不完整或越界。")
        for index in sorted(parsed):
            translations[entries[index]] = parsed[index]
    return translations, None


def _telemetry(client) -> dict:
    usage = client.usage_summary() if hasattr(client, "usage_summary") else {}
    calls = client.call_records if hasattr(client, "call_records") else []
    return {"usage": usage, "calls": calls}


##### 已授权语言动作准备板块 #####


def prepare_required_language(
    output_dir: str | Path, *, generation_id: str, template_adapter,
    heading_records: list[dict], categories: list[str], authorization, client, on_task_report=None,
) -> dict:
    """确定性外层准备派生语言改动；不写文件，不注册为模型可选工具。

    全部权限和 staging 归属先于真实调用检查。返回经过原文保护验证的动作，
    由同一文件日志提交与回滚，完整门禁通过前不能据此发布。
    """
    from agents.quality_repair.patching import writable_path, PatchSafetyError
    from agents.quality_repair.runtime import action_signature
    from pipeline.text_processing import escape_text

    task_names = {'heading': HEADING_TRANSLATION_TASK, 'caption': CAPTION_TRANSLATION_TASK}
    if len(categories) != len(set(categories)) or set(categories) - task_names.keys():
        raise ValueError('模板语言类别无效或重复。')
    root = Path(output_dir).resolve()
    collected = []
    originals = {}
    for category in categories:
        slots, entries = _collect_missing_language_inputs(root, template_adapter, heading_records, category=category)
        if not entries:
            continue
        task = task_names[category]
        if not authorization.external_processing_allowed or not authorization.allows_task(task):
            raise ValueError('模板必需语言任务尚未获得本次明确授权。')
        if not getattr(client, 'configured', False):
            raise ValueError('模板语言客户端不可用。')
        for slot in slots:
            path = writable_path(root, slot.relative_path, generation_id)
            content_root = (root / template_adapter.content_directory).resolve()
            if content_root not in path.parents:
                raise PatchSafetyError('派生语言只能位于生成的章节内容中。')
            originals[slot.relative_path] = path.read_bytes()
        collected.append((task, slots, entries))
    if not collected:
        return {'actions': [], 'tasks': []}

    all_slots, translations, reports = [], {}, []
    for task, slots, entries in collected:
        report = {'task': task, 'status': 'failed', 'requested_count': len(entries),
            'completed_count': 0, 'blocking': True, 'external_call_attempted': True,
            'updated_files': [], 'input_hashes': _input_hashes(entries),
            'detail': '模板必需英文未能通过校验，原始内容保持不变。'}
        try:
            values, error = _translate_all(client, authorization, entries, task=task)
            if error is not None:
                raise error
            for item in entries:
                value = values.get(item, '')
                if not _valid_english_text(item[1], value) or re.search(r'[\\{}]', value):
                    raise ValueError('模板语言必须是可验证的普通英文文字，不能包含 LaTeX 命令或分组。')
                translations[item] = escape_text(value)
            all_slots.extend(slots)
            report.update(status='succeeded', completed_count=len(entries), blocking=False,
                updated_files=sorted({slot.relative_path for slot in slots}),
                detail='模板必需英文已准备，等待完整校验。')
        finally:
            # 失败也保留调用事实；不把异常响应或论文原文写入用户报告。
            report.update(_telemetry(client))
            reports.append(report)
            if on_task_report is not None:
                on_task_report(dict(report))

    actions = []
    for edit in template_adapter.build_language_edits(root, all_slots, translations):
        original = originals.get(edit.relative_path)
        path = writable_path(root, edit.relative_path, generation_id)
        if original is None or path.read_bytes() != original or original.decode('utf-8').replace('\r\n', '\n') != edit.old_text:
            raise PatchSafetyError('语言准备期间来源已经变化或修改目标不在原始插槽中。')
        original_text = original.decode('utf-8')
        updated_text = edit.new_text.replace('\n', '\r\n') if b'\r\n' in original else edit.new_text
        if template_adapter.mask_language_slots(original_text) != template_adapter.mask_language_slots(updated_text):
            raise PatchSafetyError('语言修改超出了适配器声明的派生插槽。')
        digest = hashlib.sha256(original).hexdigest()
        proposal = {'kind': 'template_language', 'relative_path': edit.relative_path, 'input_sha256': digest}
        identifier = action_signature('template_language', [edit.relative_path], proposal, digest)
        actions.append({'action_id': identifier, 'proposal': proposal, 'prepared': {
            'relative_path': edit.relative_path, 'unit_id': 'language:' + edit.relative_path,
            'before': original, 'after': updated_text.encode('utf-8'),
            'old_fragment': original_text, 'new_fragment': updated_text}})
    if not actions:
        raise ValueError('模板语言任务没有生成实际插槽变化。')
    return {'actions': actions, 'tasks': reports}


##### 门禁探测板块 #####


def _probe_required_text(
    output_dir: str | Path,
    *,
    template_adapter,
    heading_records: list[dict],
    category: str,
    task: str,
) -> LLMTaskResult:
    _, entries = _collect_missing_language_inputs(
        output_dir, template_adapter, heading_records, category=category,
    )
    if not entries:
        return LLMTaskResult(
            task=task,
            status=STATUS_SKIPPED_NOT_NEEDED,
            detail="当前模板没有缺失的必需英文插槽。",
        )
    result = LLMTaskResult.disabled(task, len(entries))
    result.blocking = False
    result.input_hashes = _input_hashes(entries)
    result.detail = "发现模板英文插槽；本轮授权后由统一 Agent 补齐，否则作为降级说明。"
    return result


def translate_headings_for_toc(
    output_dir: str | Path,
    *,
    profile=None,
    template_adapter,
    heading_records: list[dict],
    **_ignored,
) -> LLMTaskResult:
    """兼容现有调用名，仅探测英文目录缺口，不直接调用模型。"""
    del profile
    return _probe_required_text(
        output_dir,
        template_adapter=template_adapter,
        heading_records=heading_records,
        category="heading",
        task=HEADING_TRANSLATION_TASK,
    )


def translate_required_captions(
    output_dir: str | Path,
    *,
    profile=None,
    template_adapter,
    heading_records: list[dict] | None = None,
    **_ignored,
) -> LLMTaskResult:
    """兼容现有调用名，仅探测英文题注缺口，不直接调用模型。"""
    del profile
    return _probe_required_text(
        output_dir,
        template_adapter=template_adapter,
        heading_records=heading_records or [],
        category="caption",
        task=CAPTION_TRANSLATION_TASK,
    )
