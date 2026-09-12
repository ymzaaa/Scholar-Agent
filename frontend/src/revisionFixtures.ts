import type { RevisionContext, RevisionSession } from './api'

// ##### 修订公共契约夹具板块 #####

export function revisionSessionFixture(overrides: Partial<RevisionSession> = {}): RevisionSession {
  return {
    session_id: 'session', project_id: 'project', base_version_id: 'parent', current_version_id: null,
    status: 'needs_user_input', state_revision: 1, current_run_id: 'run', current_run_revision: 2,
    current_task_id: null, feedbacks: [{ feedback_id: 'feedback', text: '取消首行缩进。',
      selected_unit_ids: ['body'], status: 'pending', detail: '', target_references: [] }],
    unresolved: [], evaluation_summary: {}, constraint_candidates: [], goals: [], goal_drafts: [],
    detail: '', created_at: '', updated_at: '', ...overrides,
  }
}

export function revisionContextFixture(): RevisionContext {
  return { targets: [{ unit_id: 'body', label: '原始正文。', text: '原始正文。', body_replace_allowed: true }],
    model: { provider: 'OpenAI 兼容服务', name: 'test-model', configured: true } }
}
