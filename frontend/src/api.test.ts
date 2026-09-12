import { afterEach, describe, expect, it, vi } from 'vitest'
import { confirmStructure, createProject, resumeRecognitionReview } from './api'
import { triggerGeneration, getGeneration, getGenerationReport, artifactUrl, listVersions, acceptVersion } from './api'
import { generationFixture } from './workspaceFixtures'
import type { StructureSnapshot } from './api'
import { processRevision, resumeRevision, getRevisionContext } from './api'
import { revisionSessionFixture, revisionContextFixture } from './revisionFixtures'

const snapshot: StructureSnapshot = {
  project_id: 'p1', source_sha256: 'abc', schema_version: '1.7.0', revision: 3,
  confirmed: false, headings: [], object_bindings: [{ caption_unit_id: 'c1', kind: 'table', number: '表1-1', caption: '数据', status: 'bound', object_unit_ids: ['t1'], overridden: true }], counts: {}, issues: [],
  formula_review: { status: 'passed', blocking: false, valid_formula_count: 0, malformed_formula_count: 0, candidates: [] },
  citation_review: { counts: {}, all_candidates_classified: true, decisions: [
    { unit_id: 'u-1', raw: '[1]', numbers: ['1'], decision: 'non_citation', overridden: true, source: {}, evidence: [] },
    { unit_id: 'u-2', raw: '[2]', numbers: ['2'], decision: 'body_citation', source: {}, evidence: [] },
  ] },
}

describe('引用产品接口', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('创建项目固定使用 Word 文末列表且不发送兼容上传字段', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
    vi.stubGlobal('fetch', fetcher)
    await createProject({ docx: new File(['x'], 'paper.docx'), templateId: 'ouc-graduate' })
    const form = fetcher.mock.calls[0][1].body as FormData
    expect(form.get('reference_source')).toBe('word_list')
    expect(form.has('bib')).toBe(false)
    expect(form.has('citation_map')).toBe(false)
  })

  it('普通结构提交只发送用户引用决定和当前修订号', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => snapshot })
    vi.stubGlobal('fetch', fetcher)
    await confirmStructure(snapshot)
    const body = JSON.parse(fetcher.mock.calls[0][1].body)
    expect(body.base_revision).toBe(3)
    expect(body.object_binding_overrides).toEqual([{ caption_unit_id: 'c1', object_unit_ids: ['t1'] }])
    expect(body.citation_overrides).toEqual([{ unit_id: 'u-1', decision: 'non_citation' }])
  })

  it('标题审查恢复沿用同一份用户引用决定', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
    vi.stubGlobal('fetch', fetcher)
    await resumeRecognitionReview({ run_id: 'r1', project_id: 'p1', mode: 'recognition_review', status: 'waiting_user', state_revision: 2, base_revision: 3, suggestions: [], model_status: 'ready', detail: '' }, {}, snapshot)
    const body = JSON.parse(fetcher.mock.calls[0][1].body)
    expect(body.expected_state_revision).toBe(2)
    expect(body.object_binding_overrides).toEqual([{ caption_unit_id: 'c1', object_unit_ids: ['t1'] }])
    expect(body.citation_overrides).toEqual([{ unit_id: 'u-1', decision: 'non_citation' }])
  })
})

// ##### 版本与交付契约板块 #####

describe('首次生成与版本接口', () => {
  afterEach(() => vi.unstubAllGlobals())
  it('具体修复只发送本次列明的任务，不扩展为通用授权', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ generation_id: 'g2', task_id: 't2' }) })
    vi.stubGlobal('fetch', fetcher)
    await triggerGeneration('p1', 2, ['initial_generation_repair'])
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({
      structure_revision: 2, external_processing_allowed: true,
      allowed_llm_tasks: ['initial_generation_repair'],
    })
  })
  it('默认生成请求明确不授权外部模型', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ generation_id: 'g1', task_id: 't1' }) })
    vi.stubGlobal('fetch', fetcher)
    await triggerGeneration('p1', 2)
    expect(fetcher.mock.calls[0][0]).toContain('/api/projects/p1/generate')
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({
      structure_revision: 2, external_processing_allowed: false, allowed_llm_tasks: [],
    })
  })
  it('generation 公共字段保留空值、可信状态与哈希', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => generationFixture() }))
    const result = await getGeneration('g1')
    expect(result).toEqual(generationFixture())
    expect(result.task_id).toBeNull()
    expect(result.parent_version_id).toBeNull()
    expect(result.artifacts[0]?.sha256).toHaveLength(64)
  })
  it('报告名称完整编码，产物使用正式地址', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
    vi.stubGlobal('fetch', fetcher)
    await getGenerationReport('g1', '质量 #1.json')
    expect(fetcher.mock.calls[0][0]).toBe(artifactUrl('g1', 'report', '质量 #1.json'))
    expect(fetcher.mock.calls[0][0]).toContain(encodeURIComponent('质量 #1.json'))
    expect(artifactUrl('g1', 'pdf')).toContain('/api/generations/g1/pdf')
    expect(artifactUrl('g1', 'latex_source')).toContain('/latex-source')
  })
  it('版本列表与接受响应按后端原义返回', async () => {
    const accepted = { project_id: 'p1', version_id: 'g1', accepted_version_id: 'g1',
      previous_accepted_version_id: null, review_status: 'accepted' }
    const fetcher = vi.fn().mockResolvedValueOnce({ ok: true, json: async () => ({
      project_id: 'p1', accepted_version_id: null, versions: [generationFixture()],
    }) }).mockResolvedValueOnce({ ok: true, json: async () => accepted })
    vi.stubGlobal('fetch', fetcher)
    expect((await listVersions('p1')).accepted_version_id).toBeNull()
    expect(await acceptVersion('g1')).toEqual(accepted)
    expect(fetcher.mock.calls[1][1].method).toBe('POST')
  })
  it('保留后端面向用户的错误说明', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 409,
      json: async () => ({ detail: '请先确认结构。' }) }))
    await expect(triggerGeneration('p1', 1)).rejects.toThrow('请先确认结构。')
  })
})

// ##### 反馈目标与具体授权板块 #####

describe('修订目标契约', () => {
  afterEach(() => vi.unstubAllGlobals())
  it('理解反馈只授权一次规范化任务，不提前授权格式补丁', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
    vi.stubGlobal('fetch', fetcher)
    await processRevision('session', true)
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({
      external_processing_allowed: true, allowed_llm_tasks: ['feedback_normalization'],
    })
  })
  it('确认目标沿用同一运行修订号并发送本次具体授权', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
    vi.stubGlobal('fetch', fetcher)
    await resumeRevision(revisionSessionFixture(), { goalDecisions: [{ goal_id: 'goal' }],
      authorizeTask: 'user_feedback_patch_generation' })
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({
      expected_run_revision: 2, clarifications: {}, goal_decisions: [{ goal_id: 'goal' }],
      external_processing_allowed: true, allowed_llm_tasks: ['user_feedback_patch_generation'],
    })
  })
  it('来源选择只查询当前可信版本和脱敏模型说明', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => revisionContextFixture() })
    vi.stubGlobal('fetch', fetcher)
    expect(await getRevisionContext('project')).toEqual(revisionContextFixture())
    expect(fetcher.mock.calls[0][0]).toContain('/api/projects/project/revision-context')
  })
})
