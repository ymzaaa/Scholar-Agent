# -*- coding: utf-8 -*-
"""模板语言准备属于确定性外层，复用私有动作日志而非专用 Agent 工具。"""

import json

import pytest

from adapters.ouc import OUCTemplateAdapter
from agents.quality_repair.action_journal import FileActionJournal
from llm.policy import CAPTION_TRANSLATION_TASK, HEADING_TRANSLATION_TASK, LLMAuthorization
from pipeline import translate


##### 私有模板语言夹具板块 #####

@pytest.fixture()
def case(tmp_path):
    root = tmp_path / 'staging'
    (root / 'contents').mkdir(parents=True)
    (root / '.scholar-generation.json').write_text(json.dumps({'status': 'staging', 'generation_id': 'candidate'}))
    path = root / 'contents/section_1.tex'
    path.write_text('% SCHOLAR_TEMPLATE_HEADING_EN heading chapter\n'
                    '\\enchapter{中文标题}\n'
                    '\\figurecaption{fig:original}{中文图题}{}\n'
                    'Original body.\n', encoding='utf-8')
    records = [{'unit_id': 'heading', 'level': 'chapter', 'text_zh': '中文标题',
                'target_file': 'contents/section_1.tex'}]
    return root, path, records


class Client:
    configured = True

    def __init__(self, text='English & result'):
        self.calls = 0
        self.text = text

    def complete_text(self, system, user, **kwargs):
        self.calls += 1
        return '\n'.join(f"{line.split(' |')[0]}: {self.text}"
                         for line in user.splitlines() if line.startswith('ID='))


def prepare(case, client, *, authorization=None):
    root, _path, records = case
    return translate.prepare_required_language(root, generation_id='candidate',
        template_adapter=OUCTemplateAdapter(), heading_records=records,
        categories=['heading', 'caption'], client=client,
        authorization=authorization or LLMAuthorization.for_tasks(
            [HEADING_TRANSLATION_TASK, CAPTION_TRANSLATION_TASK],
            external_processing_allowed=True, source='test'))


##### 授权和原文保护板块 #####

def test_language_preparation_is_readonly_and_uses_existing_file_journal(case, tmp_path):
    root, path, _records = case
    original = path.read_bytes()
    client = Client()
    result = prepare(case, client)
    assert client.calls == 2 and path.read_bytes() == original
    assert len(result['actions']) == 1 and len(result['tasks']) == 2
    journal = FileActionJournal(root, tmp_path / 'journal', 'run', generation_id='candidate')
    for item in result['actions']:
        journal.apply(item['action_id'], item['prepared'], proposal=item['proposal'])
    rendered = path.read_text(encoding='utf-8')
    assert r'English \& result' in rendered
    assert '中文图题' in rendered and 'Original body.' in rendered and 'fig:original' in rendered
    assert OUCTemplateAdapter.mask_language_slots(path.read_bytes().decode()) == OUCTemplateAdapter.mask_language_slots(original.decode())
    journal.rollback_all()
    assert path.read_bytes() == original


def test_language_preparation_checks_all_task_authorizations_before_call(case):
    client = Client()
    authorization = LLMAuthorization.for_tasks([HEADING_TRANSLATION_TASK],
        external_processing_allowed=True, source='test')
    with pytest.raises(ValueError):
        prepare(case, client, authorization=authorization)
    assert client.calls == 0


@pytest.mark.parametrize('status,generation_id', [('published', 'candidate'), ('staging', 'other')])
def test_language_preparation_rejects_wrong_workspace_before_call(case, status, generation_id):
    root, path, _records = case
    original = path.read_bytes()
    (root / '.scholar-generation.json').write_text(json.dumps({'status': status, 'generation_id': generation_id}))
    client = Client()
    with pytest.raises(ValueError):
        prepare(case, client)
    assert client.calls == 0 and path.read_bytes() == original


def test_model_cannot_insert_executable_latex_through_language_slot(case):
    _root, path, _records = case
    original = path.read_bytes()
    with pytest.raises(ValueError):
        prepare(case, Client(r'English \input{private}'))
    assert path.read_bytes() == original


def test_no_missing_language_does_not_call_model(case):
    _root, path, _records = case
    path.write_text(path.read_text(encoding='utf-8').replace('中文标题', 'English title')
                    .replace('{中文图题}{}', '{中文图题}{English caption}'), encoding='utf-8')
    client = Client()
    result = prepare(case, client, authorization=LLMAuthorization.offline())
    assert result['actions'] == [] and client.calls == 0
