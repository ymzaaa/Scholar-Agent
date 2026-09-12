# -*- coding: utf-8 -*-
"""首次语法修复只能获得编译位置对应的单一已登记单元。"""

import hashlib
import json

import pytest

from agents.quality_repair import runtime
from models.render_trace import RenderRecord


##### 编译位置夹具板块 #####

@pytest.fixture()
def case(tmp_path):
    (tmp_path / '.scholar-generation.json').write_text(json.dumps({'generation_id': 'candidate', 'status': 'staging'}))
    (tmp_path / 'contents').mkdir()
    source = '% SCHOLAR_UNIT_BEGIN u-000001 body_paragraph\n\\textbf{Original text.\n% SCHOLAR_UNIT_END u-000001\n'
    path = tmp_path / 'contents/section_1.tex'
    path.write_text(source, encoding='utf-8')
    records = [RenderRecord(record_id='r-000000', marker_unit_id='u-000001', source_unit_ids=['u-000001'],
        source_order=0, role='body_paragraph', status='rendered', target_file='contents/section_1.tex',
        output_order=0).to_dict()]
    return tmp_path, path, records


def locate(case, diagnostics):
    root, _path, records = case
    return runtime.locate_initial_compile_target(root, generation_id='candidate',
        diagnostics=diagnostics, records=records)


##### 唯一来源与最小上下文板块 #####

def test_compile_context_has_current_hash_and_only_registered_unit(case):
    root, path, _records = case
    result = locate(case, './contents/section_1.tex:2: Missing } inserted.\nprivate other content')
    assert result['unit_id'] == 'u-000001'
    assert result['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result['text'] == path.read_bytes().decode().splitlines(keepends=True)[1]
    assert result['diagnostics'] == 'Missing } inserted'
    assert str(root) not in json.dumps(result)
    assert 'private other content' not in json.dumps(result)


@pytest.mark.parametrize('diagnostics', [
    'main.tex:2: Missing } inserted.',
    '../contents/section_1.tex:2: Missing } inserted.',
    'contents/section_1.tex:1: Missing } inserted.',
    'contents/section_1.tex:200: Missing } inserted.',
    'contents/section_1.tex:2: Unknown compiler failure.',
    'Missing } inserted.\nl.2 some text',
])
def test_unlocated_or_unknown_error_does_not_create_repair_scope(case, diagnostics):
    assert locate(case, diagnostics) is None


def test_multiple_affected_units_do_not_choose_an_arbitrary_target(case):
    root, path, records = case
    second = root / 'contents/section_2.tex'
    second.write_text(path.read_text(encoding='utf-8').replace('u-000001', 'u-000002'), encoding='utf-8')
    records.append({**records[0], 'marker_unit_id': 'u-000002', 'source_unit_ids': ['u-000002'],
                    'target_file': 'contents/section_2.tex', 'record_id': 'r-000001', 'output_order': 1})
    assert locate(case, 'contents/section_1.tex:2: Missing } inserted.\ncontents/section_2.tex:2: Extra }') is None
