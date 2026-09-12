# -*- coding: utf-8 -*-
"""generation 并发、局部更新和可信状态不变量。"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from persistence.store import ProjectStore
from tests.support.delivery import delivery_values


##### 项目夹具板块 #####


@pytest.fixture()
def store(tmp_path):
    result = ProjectStore(tmp_path / 'state.db')
    result.create(project_id='p1', docx_path='paper.docx', bib_path=None, citation_map_path=None,
                  template_id='ouc-bachelor', reference_source='word_list',
                  workspace_dir=str(tmp_path / 'project'), source_sha256='hash')
    return result


def create(store, name='root', parent=None):
    return store.create_generation(generation_id=name, project_id='p1', structure_revision=1,
                                   source_sha256='hash', parent_generation_id=parent)


##### 并发与写入边界板块 #####


def test_project_must_exist_before_generation(store):
    with pytest.raises(ValueError, match='项目'):
        store.create_generation(generation_id='orphan', project_id='missing', structure_revision=1, source_sha256='hash')


def test_multiple_stores_allocate_unique_version_numbers(store):
    stores = [ProjectStore(store.db_path) for _ in range(12)]
    with ThreadPoolExecutor(max_workers=6) as executor:
        records = list(executor.map(lambda pair: create(pair[1], f'g{pair[0]}'), enumerate(stores)))
    assert sorted(item.version_number for item in records) == list(range(1, 13))


def test_local_update_does_not_write_other_columns(store):
    create(store)
    with store._connect() as connection:
        connection.execute("CREATE TRIGGER detect_full_write BEFORE UPDATE OF task_id ON generations BEGIN SELECT RAISE(ABORT, 'unexpected task update'); END")
    store.update_generation('root', detail='detail only')
    assert store.get_generation('root').detail == 'detail only'


@pytest.mark.parametrize('values', [{'trusted': True}, {'trusted': True, 'status': 'failed'},
                                   {'trusted': True, 'status': 'success'}, {'review_status': 'accepted'},
                                   {'review_status': 'superseded'}])
def test_invalid_trust_or_review_combination_is_rejected(store, values):
    create(store)
    with pytest.raises(ValueError):
        store.update_generation('root', **values)


def test_pending_only_and_direct_child_acceptance(store):
    create(store)
    store.publish_trusted_generation('root', **delivery_values(store, 'root'))
    create(store, 'child', 'root')
    store.publish_trusted_generation('child', **delivery_values(store, 'child'))
    with pytest.raises(ValueError, match='根版本'):
        store.accept_generation('child')
    store.accept_generation('root')
    store.reject_generation('child')
    assert store.reject_generation('child')[0].review_status == 'rejected'
    with pytest.raises(ValueError, match='待评审'):
        store.accept_generation('child')
    create(store, 'queued')
    with pytest.raises(ValueError, match='待评审'):
        store.reject_generation('queued')
    accepted, previous = store.accept_generation('root')
    assert accepted.review_status == 'accepted' and previous == 'root'


def test_feedback_delivery_keeps_its_existing_audit_report(store):
    """反馈修订仍使用自己的正式审计报告，不要求恢复父流水线报告。"""
    from pathlib import Path
    create(store)
    values = delivery_values(store, 'root')
    audit = next(item for item in values['artifact_manifest']['artifacts'] if item['kind'] == 'report')
    old = Path(values['output_dir']) / audit['relative_path']
    old.replace(old.with_name('agent_feedback.json'))
    audit.update(name='agent_feedback.json', relative_path='reports/agent_feedback.json')
    result = store.publish_trusted_generation('root', **values)
    assert result.trusted
