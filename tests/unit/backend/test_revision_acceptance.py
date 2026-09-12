# -*- coding: utf-8 -*-
"""版本接受、保护激活与修订会话收尾必须同成同败。"""

import sqlite3

import pytest

from revision.feedback import FeedbackContractError
from constraints.models import PaperConstraint
from revision.service import accept_revision, discard_revision
from persistence.store import ProjectStore
from tests.support.delivery import delivery_values, ready_feedback_state


##### 可信候选夹具板块 #####

@pytest.fixture()
def case(tmp_path):
    store = ProjectStore(tmp_path / 'state.db')
    store.create(project_id='project', docx_path='paper.docx', bib_path=None, citation_map_path=None,
        template_id='ouc-bachelor', reference_source='word_list', workspace_dir=str(tmp_path / 'project'),
        source_sha256='source')
    store.create_generation(generation_id='parent', project_id='project', source_sha256='source', structure_revision=1)
    store.publish_trusted_generation('parent', **delivery_values(store, 'parent'))
    store.accept_generation('parent')
    session = store.create_revision_session(session_id='session', project_id='project', base_generation_id='parent',
        state={'feedbacks': [{'feedback_id': 'feedback', 'text': '保持用户确认的段落和图形格式。', 'status': 'resolved'}]})
    _register_candidate(store, 'candidate', 'parent')
    session = store.get_revision_session('session')
    store.update_revision_session('session', expected_revision=session.state_revision, status='reviewable',
                                  state=session.state, head_generation_id='candidate')
    store.create_constraint(PaperConstraint(constraint_id='protection', project_id='project',
        template_id='ouc-bachelor', raw_feedback='保持段落格式', constraint_type='paragraph_layout',
        parameters={}, description='保持用户确认的段落格式', scope_type='content_unit', scope_ref='body',
        source_feedback_id='feedback', source_session_id='session', candidate_generation_id='candidate',
        status='confirmed'))
    return store


def _register_candidate(store, generation_id, parent_id, session_id='session'):
    """候选只能通过实际反馈终验登记事务创建，保留运行与会话归属证据。"""
    run_id = 'run-' + generation_id
    delivery = delivery_values(store, generation_id, project_id='project')
    store.create_agent_run(run_id=run_id, project_id='project', parent_generation_id=parent_id,
        status='ready', state=ready_feedback_state(run_id=run_id, project_id='project', parent_id=parent_id,
            session_id=session_id, feedback_id='feedback', delivery=delivery))
    session = store.get_revision_session(session_id)
    store.update_revision_session(session_id, expected_revision=session.state_revision, status='running',
        state={**session.state, 'current_run_id': run_id}, head_generation_id=session.head_generation_id)
    return store.register_trusted_feedback(run_id=run_id,
        identity=dict(generation_id=generation_id, project_id='project', source_sha256='source',
            structure_revision=1, parent_generation_id=parent_id, change_origin='user_feedback', created_by='agent'),
        delivery=delivery)


@pytest.fixture()
def chain(case):
    _register_candidate(case, 'second-candidate', 'candidate')
    session = case.get_revision_session('session')
    case.update_revision_session('session', expected_revision=session.state_revision, status='reviewable',
                                  state=session.state, head_generation_id='second-candidate')
    case.create_constraint(PaperConstraint(constraint_id='second-protection', project_id='project',
        template_id='ouc-bachelor', raw_feedback='保持图形格式', constraint_type='paragraph_layout',
        parameters={}, description='保持用户确认的图形格式', scope_type='content_unit', scope_ref='figure',
        source_feedback_id='second-feedback', source_session_id='session',
        candidate_generation_id='second-candidate', status='confirmed'))
    return case


##### 同一事务与故障注入板块 #####

def test_session_update_failure_rolls_back_acceptance_and_constraint_activation(case):
    with sqlite3.connect(case.db_path) as connection:
        connection.execute("""CREATE TRIGGER fail_session_accept BEFORE UPDATE ON revision_sessions
            WHEN NEW.status = 'accepted' BEGIN SELECT RAISE(ABORT, 'injected session write failure'); END""")
    with pytest.raises(sqlite3.DatabaseError, match='injected session write failure'):
        accept_revision(case, 'session')
    restored = ProjectStore(case.db_path)
    assert restored.get('project').accepted_generation_id == 'parent'
    assert restored.get_generation('parent').review_status == 'accepted'
    assert restored.get_generation('candidate').review_status == 'pending'
    assert restored.get_revision_session('session').status == 'reviewable'
    assert restored.get_constraint('protection').status == 'confirmed'


def test_acceptance_atomically_closes_session_and_is_idempotent(case):
    result = accept_revision(case, 'session')
    assert result.status == 'accepted'
    assert case.get('project').accepted_generation_id == 'candidate'
    assert case.get_constraint('protection').status == 'active'
    assert case.get_generation('candidate').active_constraints[0]['constraint_id'] == 'protection'
    assert accept_revision(case, 'session') == result


def test_version_accept_entry_also_closes_its_matching_revision_session(case):
    accepted, previous = case.accept_generation('candidate')
    assert accepted.generation_id == 'candidate' and previous == 'parent'
    assert case.get_revision_session('session').status == 'accepted'
    assert case.get_constraint('protection').status == 'active'


def test_stale_session_revision_never_moves_acceptance_pointer(case):
    session = case.get_revision_session('session')
    case.update_revision_session('session', expected_revision=session.state_revision, status='reviewable',
                                  state={**session.state, 'detail': 'new state'}, head_generation_id='candidate')
    with pytest.raises(ValueError, match='状态'):
        case.accept_generation('candidate', session_id='session', expected_session_revision=session.state_revision)
    assert case.get('project').accepted_generation_id == 'parent'
    assert case.get_constraint('protection').status == 'confirmed'


def test_session_accept_cannot_target_another_version(case):
    session = case.get_revision_session('session')
    with pytest.raises(ValueError, match='会话'):
        case.accept_generation('parent', session_id='session', expected_session_revision=session.state_revision)
    assert case.get_revision_session('session').status == 'reviewable'
    assert case.get('project').accepted_generation_id == 'parent'


def test_ordinary_version_acceptance_still_requires_direct_child(chain):
    with pytest.raises(ValueError, match='直接子版本'):
        chain.accept_generation('second-candidate')
    assert chain.get('project').accepted_generation_id == 'parent'
    assert chain.get_generation('candidate').review_status == 'pending'
    assert chain.get_generation('second-candidate').review_status == 'pending'


def test_next_batch_inherits_only_its_trusted_session_chain(chain):
    from constraints.repository import inherited_for_session
    run = chain.create_agent_run(run_id='next-run', project_id='project',
        parent_generation_id='second-candidate', status='running', state={'session_id': 'session'})
    session = chain.get_revision_session('session')
    inherited = inherited_for_session(chain, run, session)
    assert {item['constraint_id'] for item in inherited} == {'protection', 'second-protection'}
    assert chain.get_constraint('protection').status == 'confirmed'
    assert chain.get('project').accepted_generation_id == 'parent'
    inherited[0]['description'] = 'caller change'
    assert chain.get_constraint(inherited[0]['constraint_id']).description != 'caller change'


def test_next_batch_rejects_broken_parent_chain_before_planning(chain):
    from constraints.repository import inherited_for_session
    run = chain.create_agent_run(run_id='next-run', project_id='project',
        parent_generation_id='second-candidate', status='running', state={'session_id': 'session'})
    with chain._connect() as connection:
        connection.execute("UPDATE generations SET trusted = 0 WHERE generation_id = 'candidate'")
    with pytest.raises(ValueError, match='保护'):
        inherited_for_session(chain, run, chain.get_revision_session('session'))


def test_session_accepts_verified_chain_and_inherits_protections(chain):
    accepted = accept_revision(chain, 'session')
    assert accepted.status == 'accepted'
    assert chain.get('project').accepted_generation_id == 'second-candidate'
    assert chain.get_generation('candidate').review_status == 'superseded'
    assert chain.get_generation('candidate').parent_generation_id == 'parent'
    assert chain.get_generation('second-candidate').parent_generation_id == 'candidate'
    assert {item['constraint_id'] for item in chain.get_generation('second-candidate').active_constraints} == {
        'protection', 'second-protection'}
    assert [item['constraint_id'] for item in chain.get_generation('candidate').active_constraints] == ['protection']
    assert accept_revision(chain, 'session') == accepted


def test_explicit_replacement_stays_temporary_until_acceptance(chain):
    import json
    from constraints.repository import inherited_for_session
    with chain._connect() as connection:
        connection.execute("UPDATE paper_constraints SET scope_ref = 'body', conflict_ids_json = ?, "
            "parameters_json = ? WHERE constraint_id = 'second-protection'",
            (json.dumps(['protection']), json.dumps({'supersedes_constraint_ids': ['protection']})))
    run = chain.create_agent_run(run_id='next-run', project_id='project',
        parent_generation_id='second-candidate', status='running', state={'session_id': 'session'})
    inherited = inherited_for_session(chain, run, chain.get_revision_session('session'))
    assert [item['constraint_id'] for item in inherited] == ['second-protection']
    assert chain.get_constraint('protection').status == 'confirmed'
    accept_revision(chain, 'session')
    assert chain.get_constraint('protection').status == 'superseded'
    assert chain.get_constraint('second-protection').status == 'active'
    assert [item['constraint_id'] for item in chain.get_generation('second-candidate').active_constraints] == ['second-protection']
    chain.rollback_accepted_generation('second-candidate')
    assert chain.get_constraint('protection').status == 'active'
    assert chain.get_constraint('second-protection').status == 'revoked'


@pytest.mark.parametrize('corruption', ['cross_session', 'missing_run', 'duplicate_run', 'rejected',
                                      'untrusted', 'changed_parent', 'changed_baseline', 'cycle', 'missing_parent',
                                      'source', 'non_feedback', 'invalid_run_state'])
def test_session_chain_rejects_invalid_ownership_or_review_state(chain, corruption):
    with chain._connect() as connection:
        if corruption == 'cross_session':
            connection.execute("UPDATE agent_runs SET state_json = '{\"session_id\":\"other\"}' WHERE candidate_generation_id = 'candidate'")
        elif corruption == 'missing_run':
            connection.execute("DELETE FROM agent_runs WHERE candidate_generation_id = 'candidate'")
        elif corruption == 'duplicate_run':
            chain.create_agent_run(run_id='duplicate', project_id='project', parent_generation_id='parent',
                candidate_generation_id='candidate', status='ready', state={'session_id': 'session'})
        elif corruption == 'changed_baseline':
            connection.execute("UPDATE projects SET accepted_generation_id = 'candidate' WHERE project_id = 'project'")
        elif corruption == 'changed_parent':
            connection.execute("UPDATE agent_runs SET parent_generation_id = 'second-candidate' WHERE candidate_generation_id = 'candidate'")
        elif corruption == 'rejected':
            connection.execute("UPDATE generations SET review_status = 'rejected' WHERE generation_id = 'candidate'")
        elif corruption == 'cycle':
            connection.execute("UPDATE generations SET parent_generation_id = 'second-candidate' WHERE generation_id = 'candidate'")
            connection.execute("UPDATE agent_runs SET parent_generation_id = 'second-candidate' WHERE candidate_generation_id = 'candidate'")
        elif corruption == 'missing_parent':
            connection.execute("UPDATE generations SET parent_generation_id = 'missing' WHERE generation_id = 'candidate'")
            connection.execute("UPDATE agent_runs SET parent_generation_id = 'missing' WHERE candidate_generation_id = 'candidate'")
        elif corruption == 'source':
            connection.execute("UPDATE generations SET source_sha256 = 'changed' WHERE generation_id = 'candidate'")
        elif corruption == 'non_feedback':
            connection.execute("UPDATE generations SET change_origin = 'initial_generation' WHERE generation_id = 'candidate'")
        elif corruption == 'invalid_run_state':
            connection.execute("UPDATE agent_runs SET state_json = '[]' WHERE candidate_generation_id = 'candidate'")
        else:
            connection.execute("UPDATE generations SET trusted = 0 WHERE generation_id = 'candidate'")
    before = chain.get('project').accepted_generation_id
    with pytest.raises(ValueError):
        accept_revision(chain, 'session')
    assert chain.get('project').accepted_generation_id == before
    assert chain.get_generation('second-candidate').review_status == 'pending'
    assert chain.get_constraint('protection').status == 'confirmed'
    assert chain.get_constraint('second-protection').status == 'confirmed'


def test_accepted_session_chain_keeps_direct_parent_rollback_and_protection_snapshot(chain):
    accept_revision(chain, 'session')
    rejected, accepted = chain.rollback_accepted_generation('second-candidate')
    assert accepted.generation_id == 'candidate' and rejected.review_status == 'rejected'
    assert chain.get('project').accepted_generation_id == 'candidate'
    assert chain.get_constraint('protection').status == 'active'
    assert chain.get_constraint('second-protection').status == 'revoked'


def test_stale_session_chain_revision_cannot_accept(chain):
    session = chain.get_revision_session('session')
    with pytest.raises(ValueError, match='状态'):
        chain.accept_generation('second-candidate', session_id='session',
                                expected_session_revision=session.state_revision - 1)
    assert chain.get('project').accepted_generation_id == 'parent'


def test_unconfirmed_inherited_protection_blocks_whole_chain(chain):
    with chain._connect() as connection:
        connection.execute("UPDATE paper_constraints SET status = 'proposed' WHERE constraint_id = 'protection'")
    with pytest.raises(ValueError, match='未确认'):
        accept_revision(chain, 'session')
    assert chain.get_generation('candidate').review_status == 'pending'
    assert chain.get('project').accepted_generation_id == 'parent'


def test_session_chain_failure_rolls_back_all_versions_and_protections(chain):
    with chain._connect() as connection:
        connection.execute("""CREATE TRIGGER fail_chain_accept BEFORE UPDATE ON revision_sessions
            WHEN NEW.status = 'accepted' BEGIN SELECT RAISE(ABORT, 'injected chain failure'); END""")
    with pytest.raises(sqlite3.DatabaseError, match='injected chain failure'):
        accept_revision(chain, 'session')
    assert chain.get('project').accepted_generation_id == 'parent'
    for identifier in ('candidate', 'second-candidate'):
        version = chain.get_generation(identifier)
        assert version.review_status == 'pending' and version.active_constraints == []
    assert chain.get_constraint('protection').status == 'confirmed'
    assert chain.get_constraint('second-protection').status == 'confirmed'


##### 整轮放弃与原子收尾板块 #####


def test_discard_rejects_every_session_candidate_and_is_idempotent(chain):
    discarded = discard_revision(chain, 'session')
    assert discarded.status == 'discarded'
    assert chain.get('project').accepted_generation_id == 'parent'
    assert chain.get_generation('parent').review_status == 'accepted'
    for identifier in ('candidate', 'second-candidate'):
        assert chain.get_generation(identifier).review_status == 'rejected'
        with pytest.raises(ValueError):
            chain.accept_generation(identifier)
    assert chain.get_constraint('protection').status == 'revoked'
    assert chain.get_constraint('second-protection').status == 'revoked'
    assert discard_revision(chain, 'session') == discarded


def test_discard_session_failure_rolls_back_all_candidates_and_protections(chain):
    with chain._connect() as connection:
        connection.execute("""CREATE TRIGGER fail_discard BEFORE UPDATE ON revision_sessions
            WHEN NEW.status = 'discarded' BEGIN SELECT RAISE(ABORT, 'injected discard failure'); END""")
    with pytest.raises(sqlite3.DatabaseError, match='injected discard failure'):
        discard_revision(chain, 'session')
    assert chain.get_revision_session('session').status == 'reviewable'
    for identifier in ('candidate', 'second-candidate'):
        assert chain.get_generation(identifier).review_status == 'pending'
    assert chain.get_constraint('protection').status == 'confirmed'
    assert chain.get_constraint('second-protection').status == 'confirmed'
    assert chain.get('project').accepted_generation_id == 'parent'


def test_accepted_session_cannot_be_discarded(chain):
    accepted = accept_revision(chain, 'session')
    with pytest.raises(ValueError, match='接受'):
        discard_revision(chain, 'session')
    assert chain.get_revision_session('session') == accepted
    assert chain.get_constraint('protection').status == 'active'
    assert chain.get_constraint('second-protection').status == 'active'
    assert chain.get('project').accepted_generation_id == 'second-candidate'


def test_discard_rejects_cross_session_candidate_without_partial_updates(chain):
    with chain._connect() as connection:
        connection.execute("UPDATE agent_runs SET state_json = '{\"session_id\":\"other\"}' "
                           "WHERE candidate_generation_id = 'candidate'")
    with pytest.raises(ValueError, match='会话'):
        discard_revision(chain, 'session')
    assert chain.get_revision_session('session').status == 'reviewable'
    assert chain.get_generation('second-candidate').review_status == 'pending'
    assert chain.get_constraint('second-protection').status == 'confirmed'


def test_discard_collecting_session_keeps_accepted_baseline(case):
    discard_revision(case, 'session')
    case.create_revision_session(session_id='empty', project_id='project', base_generation_id='parent', state={})
    assert discard_revision(case, 'empty').status == 'discarded'
    assert case.get_generation('parent').review_status == 'accepted'


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_discard_never_interrupts_an_executing_batch(case, status):
    session = case.get_revision_session('session')
    case.update_revision_session('session', expected_revision=session.state_revision, status=status,
                                  state=session.state, head_generation_id=session.head_generation_id)
    with pytest.raises(FeedbackContractError, match='运行'):
        discard_revision(case, 'session')
    assert case.get_revision_session('session').status == status


def test_multi_target_goal_replaces_only_protections_of_each_target(chain):
    import json
    from constraints.repository import insert_completed_protections

    with chain._connect() as connection:
        run = connection.execute("SELECT * FROM agent_runs WHERE run_id = 'run-second-candidate'").fetchone()
        session = connection.execute("SELECT * FROM revision_sessions WHERE session_id = 'session'").fetchone()
        state = json.loads(run['state_json'])
        agent = state['agent']
        agent['goals'][0]['target_unit_ids'] = ['body', 'figure']
        agent['goals'][0]['supersedes_constraint_ids'] = ['protection', 'second-protection']
        agent['protections'] = [{'unit_id': unit, 'action_id': unit + '-action',
            'goal_ids': [agent['goals'][0]['goal_id']]} for unit in ('body', 'figure')]
        agent['observations'] = [{'unit_id': unit, 'action_id': unit + '-action', 'status': 'ok',
            'tool': 'apply_protected_patch', 'changed_files': ['chapter.tex']} for unit in ('body', 'figure')]
        insert_completed_protections(connection, run=run, state=state, session=session,
                                      generation_id='next', now='2026-09-10')
    assert chain.get_constraint('next:body').conflict_ids == ('protection',)
    assert chain.get_constraint('next:figure').conflict_ids == ('second-protection',)
    assert chain.get_constraint('protection').status == 'confirmed'
