# -*- coding: utf-8 -*-
"""P9-A：通用层不依赖 OUC 宏的模板语言插槽契约。"""

from __future__ import annotations

import re
import json
from pathlib import Path

import pytest

from agents.quality_repair.patching import PatchSafetyError
from llm.policy import CAPTION_TRANSLATION_TASK, LLMAuthorization
from pipeline.template_language import TemplateLanguageEdit, TemplateLanguageSlot
from pipeline.translate import translate_required_captions, prepare_required_language
from tests.support.fake_llm import FakeLLMClient


##### 合成适配器板块 #####


class _BracketTemplateAdapter:
    """使用非 LaTeX 合成语法，证明核心没有识别 OUC 命令。"""

    pattern = re.compile(r"\[\[CAPTION:([^|]+)\|\|([^]]*)\]\]")
    content_directory = 'sections'

    def discover_language_slots(self, output_dir, heading_records):
        del heading_records
        root = Path(output_dir).resolve()
        path = root / 'sections/paper.tex'
        text = path.read_text(encoding="utf-8")
        return [
            TemplateLanguageSlot(
                slot_id=f"caption:{index}", category="caption",
                kind="figure_caption", source_text=match.group(1),
                current_text=match.group(2), relative_path="sections/paper.tex",
                payload={"start": match.start(), "end": match.end()},
            )
            for index, match in enumerate(self.pattern.finditer(text))
        ]

    def build_language_edits(self, output_dir, slots, translations):
        path = Path(output_dir).resolve() / "sections/paper.tex"
        source = path.read_text(encoding="utf-8")
        updated = source
        for slot in sorted(slots, key=lambda item: item.payload["start"], reverse=True):
            value = translations[(slot.kind, slot.source_text)]
            replacement = f"[[CAPTION:{slot.source_text}||{value}]]"
            updated = (
                updated[:slot.payload["start"]]
                + replacement
                + updated[slot.payload["end"]:]
            )
        return [TemplateLanguageEdit("sections/paper.tex", source, updated)]

    def mask_language_slots(self, text):
        return self.pattern.sub(lambda match: f"[[CAPTION:{match.group(1)}||__TARGET__]]", text)


class _Profile:
    pass


def _authorization():
    return LLMAuthorization.for_tasks(
        [CAPTION_TRANSLATION_TASK], external_processing_allowed=True, source="p9-a-test",
    )


##### 通用契约验证板块 #####


def test_non_ouc_adapter_can_expose_language_gap_without_core_changes(tmp_path: Path) -> None:
    (tmp_path / 'sections').mkdir()
    path = tmp_path / "sections/paper.tex"
    path.write_text("正文\n[[CAPTION:细胞空间图||]]\n结尾", encoding="utf-8")
    adapter = _BracketTemplateAdapter()
    result = translate_required_captions(
        tmp_path, profile=_Profile(), template_adapter=adapter,
        authorization=_authorization(),
        client=FakeLLMClient(["ID=0: Cell Spatial Graph"]),
    )
    assert result.status == "disabled"
    assert "[[CAPTION:细胞空间图||]]" in path.read_text(encoding="utf-8")


def test_adapter_mask_allows_only_declared_language_parameter(tmp_path: Path) -> None:
    (tmp_path / 'sections').mkdir()
    (tmp_path / '.scholar-generation.json').write_text(json.dumps({'status': 'staging', 'generation_id': 'candidate'}))
    path = tmp_path / "sections/paper.tex"
    old = "正文\n[[CAPTION:细胞空间图||]]"
    path.write_text(old, encoding="utf-8")
    adapter = _BracketTemplateAdapter()
    values = dict(generation_id='candidate', template_adapter=adapter, heading_records=[],
                  categories=['caption'], authorization=_authorization())
    prepared = prepare_required_language(tmp_path, **values, client=FakeLLMClient(['ID=0: Cell Spatial Graph']))
    assert prepared['actions'][0]['prepared']['new_fragment'] == path.read_bytes().decode('utf-8').replace(
        '[[CAPTION:细胞空间图||]]', '[[CAPTION:细胞空间图||Cell Spatial Graph]]')
    original_builder = adapter.build_language_edits
    def outside_slot(output_dir, slots, translations):
        edits = original_builder(output_dir, slots, translations)
        return [TemplateLanguageEdit(edit.relative_path, edit.old_text, edit.new_text.replace('正文', '正文已改写'))
                for edit in edits]
    adapter.build_language_edits = outside_slot
    with pytest.raises(PatchSafetyError, match="插槽"):
        prepare_required_language(tmp_path, **values, client=FakeLLMClient(['ID=0: Cell Spatial Graph']))
    assert path.read_text(encoding='utf-8') == old
