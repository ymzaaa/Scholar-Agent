# -*- coding: utf-8 -*-
"""正式渲染入口必须在复制模板前执行输出目录安全检查。"""

from pathlib import Path

import pytest

from pipeline.results import ExtractResult, RecognizeResult
from pipeline.workspace import WorkspaceSafetyError
from pipeline_api import PipelineRunner


##### 渲染安全板块 #####


def test_pipeline_render_checks_before_copying_template(tmp_path: Path) -> None:
    output = tmp_path / "published"
    output.mkdir()
    sentinel = output / "main.tex"
    sentinel.write_text("published", encoding="utf-8")
    from tests.unit.pipeline.test_confirmed_structure_contract import prepare
    extracted, recognized, _, _ = prepare(tmp_path)

    with pytest.raises(WorkspaceSafetyError):
        PipelineRunner().render(
            extracted,
            recognized,
            str(tmp_path / "missing-template"),
            str(output),
            str(tmp_path / "missing.bib"),
        )
    assert sentinel.read_text(encoding="utf-8") == "published"
