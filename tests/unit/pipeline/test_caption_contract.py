# -*- coding: utf-8 -*-
"""模板门控双语题注的识别、渲染和模型补全测试。"""

from pathlib import Path

from adapters.ouc import OUCTemplateAdapter
from adapters.ouc_bachelor import OUCBachelorTemplateAdapter
from llm.policy import CAPTION_TRANSLATION_TASK, LLMAuthorization
from pipeline.structure_recognizer import infer_english_caption
from pipeline.translate import translate_required_captions
from tests.support.fake_llm import FakeLLMClient


##### 测试画像板块 #####


class _Profile:
    def __init__(self, required: bool) -> None:
        self.required = required

    def requires_english_caption(self, _kind: str) -> bool:
        return self.required


def _authorization() -> LLMAuthorization:
    return LLMAuthorization.for_tasks(
        [CAPTION_TRANSLATION_TASK], external_processing_allowed=True, source="test",
    )


##### 题注行为板块 #####


def test_existing_word_english_caption_is_recognized() -> None:
    caption = infer_english_caption("Fig. 3-5 Violin Plot of Cell Features")
    assert caption == {
        "kind": "figure", "number": "图3-5",
        "caption": "Violin Plot of Cell Features",
    }


def test_required_missing_caption_is_deferred_to_agent(tmp_path: Path) -> None:
    contents = tmp_path / "contents"
    contents.mkdir()
    path = contents / "section_01.tex"
    path.write_text(
        "\\figurecaption{细胞图}{细胞图}{}\n\\tablecaption{样本表}{样本表}",
        encoding="utf-8",
    )
    client = FakeLLMClient([
        "ID=0: Cell Feature Plot\nID=1: Sample Information",
    ])
    result = translate_required_captions(
        tmp_path, profile=_Profile(True), template_adapter=OUCTemplateAdapter(),
        authorization=_authorization(), client=client,
    )
    updated = path.read_text(encoding="utf-8")
    assert result.status == "disabled"
    assert result.requested_count == 2
    assert result.blocking is False
    assert client.calls == 0
    assert updated == "\\figurecaption{细胞图}{细胞图}{}\n\\tablecaption{样本表}{样本表}"


def test_template_without_english_requirement_does_not_call_model(tmp_path: Path) -> None:
    contents = tmp_path / "contents"
    contents.mkdir()
    (contents / "section_01.tex").write_text(
        "\\figurecaption{图}{图}{}", encoding="utf-8",
    )
    client = FakeLLMClient()
    result = translate_required_captions(
        tmp_path, profile=_Profile(False), template_adapter=OUCBachelorTemplateAdapter(),
        authorization=_authorization(), client=client,
    )
    assert result.status == "skipped_not_needed"
    assert client.calls == 0


def test_probe_never_consumes_model_response_or_modifies_file(tmp_path: Path) -> None:
    contents = tmp_path / "contents"
    contents.mkdir()
    path = contents / "section_01.tex"
    original = "\\figurecaption{细胞图}{细胞图}{}"
    path.write_text(original, encoding="utf-8")
    client = FakeLLMClient(["ID=0: 细胞图"])
    result = translate_required_captions(
        tmp_path, profile=_Profile(True), template_adapter=OUCTemplateAdapter(),
        authorization=_authorization(),
        client=client,
    )
    assert result.status == "disabled"
    assert result.blocking is False
    assert client.calls == 0
    assert path.read_text(encoding="utf-8") == original
