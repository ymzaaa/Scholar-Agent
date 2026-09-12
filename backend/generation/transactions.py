# -*- coding: utf-8 -*-
"""用户接受版本的 SQLite 事务；版本、活动约束和修订会话同时提交。"""

from datetime import datetime, timezone
import json

from constraints import repository as constraint_repository


##### 会话一致性板块 #####

def _accepting_session(connection, target, project, *, session_id, expected_revision):
    """版本入口也收尾所属修订会话，防止不同接受入口产生不同状态。"""
    if session_id is not None:
        session = connection.execute('SELECT * FROM revision_sessions WHERE session_id = ?', (session_id,)).fetchone()
        if (session is None or session['project_id'] != project['project_id']
            or session['head_generation_id'] != target['generation_id']):
            raise ValueError('接受版本与修订会话不一致。')
        if session['state_revision'] != expected_revision:
            raise ValueError('修订会话状态已变化，请刷新后重试。')
    else:
        matches = connection.execute("""SELECT * FROM revision_sessions
            WHERE project_id = ? AND head_generation_id = ? AND status NOT IN ('accepted', 'discarded')""",
            (project['project_id'], target['generation_id'])).fetchall()
        if len(matches) > 1:
            raise ValueError('候选属于多个未结束修订会话。')
        session = matches[0] if matches else None
    if session is not None:
        if session['status'] == 'accepted' and project['accepted_generation_id'] == target['generation_id']:
            return session
        if session['status'] != 'reviewable' or session['base_generation_id'] != project['accepted_generation_id']:
            raise ValueError('修订会话尚不可评审或其接受基线已经变化。')
    return session


##### 用户接受事务板块 #####

def _session_candidates(store, connection, target, project, session):
    """沿不可变父链核验同会话候选，按反馈发生顺序返回，禁止跨会话继承接受权限。"""
    candidates = []
    seen = set()
    current = target
    baseline = project['accepted_generation_id']
    while current is not None and current['generation_id'] != baseline:
        identifier = current['generation_id']
        if (identifier in seen or current['project_id'] != project['project_id']
            or current['source_sha256'] != project['source_sha256']
            or current['change_origin'] != 'user_feedback'
            or not current['trusted'] or current['status'] not in {'success', 'degraded'}
            or current['review_status'] != 'pending'):
            raise ValueError('会话候选链必须全部为本项目未拒绝的可信待评审反馈版本。')
        seen.add(identifier)
        store._validate_trusted_row(connection, current)
        runs = connection.execute('SELECT * FROM agent_runs WHERE candidate_generation_id = ?',
                                  (identifier,)).fetchall()
        if len(runs) != 1:
            raise ValueError('会话候选缺少唯一的反馈运行归属。')
        run = runs[0]
        state = json.loads(run['state_json'])
        if (run['mode'] != 'user_feedback' or run['project_id'] != project['project_id']
            or run['parent_generation_id'] != current['parent_generation_id']
            or not isinstance(state, dict) or state.get('session_id') != session['session_id']):
            raise ValueError('候选反馈运行与当前修订会话或父版本不一致。')
        candidates.append(current)
        current = connection.execute('SELECT * FROM generations WHERE generation_id = ?',
                                     (current['parent_generation_id'],)).fetchone()
    if (current is None or not baseline or current['project_id'] != project['project_id']
        or not current['trusted'] or current['status'] not in {'success', 'degraded'}
        or current['review_status'] != 'accepted'):
        raise ValueError('会话候选链无法回溯到当前可信接受基线。')
    store._validate_trusted_row(connection, current)
    return list(reversed(candidates))


def accept_generation(store, generation_id: str, *, session_id=None, expected_session_revision=None):
    """接受指针、保护激活和会话收尾共用 BEGIN IMMEDIATE；任何失败全部回滚。"""
    now = datetime.now(timezone.utc).isoformat()
    with store._write_lock, store._connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        target = connection.execute('SELECT * FROM generations WHERE generation_id = ?', (generation_id,)).fetchone()
        if target is None:
            raise KeyError(generation_id)
        if not target['trusted'] or target['status'] not in {'success', 'degraded'}:
            raise ValueError('只有已发布的可信版本可以被用户接受。')
        project = connection.execute('SELECT * FROM projects WHERE project_id = ?', (target['project_id'],)).fetchone()
        if project is None:
            raise ValueError('版本所属项目不存在。')
        store._validate_trusted_row(connection, target)
        session = _accepting_session(connection, target, project,
            session_id=session_id, expected_revision=expected_session_revision)
        previous = project['accepted_generation_id']
        if previous != generation_id:
            if target['review_status'] != 'pending':
                raise ValueError('只有待评审版本可以接受。')
            if not previous and target['parent_generation_id']:
                raise ValueError('首次接受只能选择根版本。')
            candidates = [target]
            if previous and target['parent_generation_id'] != previous:
                if session_id is None:
                    raise ValueError('候选版本不是当前接受版本的直接子版本。')
                candidates = _session_candidates(store, connection, target, project, session)
            # 中间版本只标记已替代；保存其保护快照以支持原有的直接父版本回退。
            for candidate in candidates:
                active = constraint_repository.activate_for_generation(connection, project=project, generation=candidate, now=now)
                if candidate['generation_id'] != generation_id:
                    connection.execute("""UPDATE generations SET review_status = 'superseded',
                        active_constraints_json = ?, updated_at = ? WHERE generation_id = ?""",
                        (json.dumps(active, ensure_ascii=False), now, candidate['generation_id']))
            if previous:
                connection.execute("UPDATE generations SET review_status = 'superseded', updated_at = ? WHERE generation_id = ?",
                                   (now, previous))
            connection.execute("""UPDATE generations SET review_status = 'accepted', active_constraints_json = ?, updated_at = ?
                WHERE generation_id = ?""", (json.dumps(active, ensure_ascii=False), now, generation_id))
            connection.execute('UPDATE projects SET accepted_generation_id = ?, updated_at = ? WHERE project_id = ?',
                               (generation_id, now, target['project_id']))
        if session is not None and session['status'] != 'accepted':
            state = json.loads(session['state_json'])
            state['detail'] = '当前版本已接受。'
            connection.execute("""UPDATE revision_sessions SET status = 'accepted', state_json = ?,
                state_revision = state_revision + 1, updated_at = ? WHERE session_id = ?""",
                (json.dumps(state, ensure_ascii=False), now, session['session_id']))
    accepted = store.get_generation(generation_id)
    if accepted is None:
        raise RuntimeError('接受版本事务完成后无法读取目标版本。')
    return accepted, previous


##### 整轮放弃事务板块 #####


def discard_revision_session(store, session_id, *, expected_revision):
    """仅收尾本会话完整可信候选链；拒绝、临时保护撤销及会话关闭同时提交。"""
    now = datetime.now(timezone.utc).isoformat()
    with store._write_lock, store._connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        session = connection.execute('SELECT * FROM revision_sessions WHERE session_id = ?', (session_id,)).fetchone()
        if session is None:
            raise KeyError(session_id)
        if session['status'] == 'discarded':
            return store._revision_session_from_row(session)
        if session['state_revision'] != expected_revision:
            raise ValueError('修订会话状态已变化，请刷新后重试。')
        if session['status'] in {'queued', 'running'}:
            raise ValueError('Agent 运行期间不能放弃修订。')
        if session['status'] == 'accepted':
            raise ValueError('已接受的修订会话不能放弃，请使用版本回退。')
        project = connection.execute('SELECT * FROM projects WHERE project_id = ?', (session['project_id'],)).fetchone()
        if project is None or project['accepted_generation_id'] != session['base_generation_id']:
            raise ValueError('修订会话的接受基线已经变化。')
        candidates = []
        if session['head_generation_id']:
            target = connection.execute('SELECT * FROM generations WHERE generation_id = ?',
                                        (session['head_generation_id'],)).fetchone()
            candidates = _session_candidates(store, connection, target, project, session)
        for candidate in candidates:
            connection.execute("""UPDATE generations SET review_status = 'rejected', detail = ?, updated_at = ?
                WHERE generation_id = ?""", ('用户已放弃该技术可信修订候选。', now, candidate['generation_id']))
        connection.execute("""UPDATE paper_constraints SET status = 'revoked', updated_at = ?
            WHERE project_id = ? AND source_session_id = ? AND status IN ('confirmed', 'proposed')""",
            (now, session['project_id'], session_id))
        state = {**json.loads(session['state_json']), 'detail': '已放弃本轮修订，接受版本未改变。'}
        connection.execute("""UPDATE revision_sessions SET status = 'discarded', state_json = ?,
            state_revision = state_revision + 1, updated_at = ? WHERE session_id = ?""",
            (json.dumps(state, ensure_ascii=False), now, session_id))
    return store.get_revision_session(session_id)
