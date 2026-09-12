import type { Generation, StructureSnapshot, InitialRepairRequest } from './api'

// ##### 正式公共契约夹具板块 #####

export function initialRepairFixture(): InitialRepairRequest {
  return { generation_id: 'g1', model: { provider: 'OpenAI 兼容服务', name: 'test-model', configured: true },
    tasks: [{ task: 'initial_generation_repair', title: '修复已定位的编译语法问题',
      data_scope: '局部编译诊断及已定位的内容单元。', item_count: 1 }], max_calls: 3 }
}

export function generationFixture(overrides: Partial<Generation> = {}): Generation {
  return {
    generation_id: 'g1', project_id: 'p1', structure_revision: 1,
    source_sha256: 'a'.repeat(64), task_id: null, status: 'success',
    quality_status: 'passed', detail: '', parent_version_id: null,
    version_number: 1, change_origin: 'initial_generation', created_by: 'pipeline',
    trusted: true, review_status: 'pending', accepted: false, rollback_target_id: null,
    active_constraints: [], gate_snapshot: {}, created_at: '', updated_at: '',
    artifacts: [
      { kind: 'pdf', name: 'main.pdf', root: 'output', relative_path: 'main.pdf', size: 100, sha256: 'b'.repeat(64) },
      { kind: 'latex_source', name: 'latex-source.zip', root: 'output', relative_path: 'latex-source.zip', size: 100, sha256: 'c'.repeat(64) },
      { kind: 'report', name: 'pipeline_log.json', root: 'report', relative_path: 'pipeline_log.json', size: 100, sha256: 'd'.repeat(64) },
    ],
    ...overrides,
  }
}

export function structureFixture(confirmed = false): StructureSnapshot {
  return {
    project_id: 'p1', source_sha256: 'a'.repeat(64), schema_version: '1.7.0',
    revision: confirmed ? 1 : 0, confirmed,
    headings: [{ unit_id: 'h1', text: '第一章 绪论', level: 'chapter', suggested_level: 'chapter',
      confidence: 1, evidence: [], requires_review: false, review_status: 'auto_resolved' }],
    object_bindings: [], counts: { headings: 1, content_units: 1, object_bindings: 0 }, issues: [],
    citation_review: { decisions: [], counts: {}, all_candidates_classified: true },
    formula_review: { status: 'passed', blocking: false, valid_formula_count: 0, malformed_formula_count: 0, candidates: [] },
  }
}
