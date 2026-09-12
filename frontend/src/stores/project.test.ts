import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { generationFixture, structureFixture, initialRepairFixture } from '../workspaceFixtures'
import { useProjectStore, GENERATION_POLL_LIMIT, GENERATION_POLL_INTERVAL_MS } from './project'

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return { ...actual, createProject: vi.fn(), triggerExtract: vi.fn(), getTask: vi.fn(),
    getStructure: vi.fn(), confirmStructure: vi.fn(), triggerGeneration: vi.fn(),
    getGeneration: vi.fn(), getGenerationReport: vi.fn(), acceptVersion: vi.fn(),
    startRecognitionReview: vi.fn(), getRecognitionReview: vi.fn(), resumeRecognitionReview: vi.fn() }
})

// ##### 首次转换契约板块 #####

describe('首次转换工作区', () => {
  beforeEach(() => {
    sessionStorage.clear()
    setActivePinia(createPinia())
    vi.resetAllMocks()
    vi.mocked(api.getStructure).mockResolvedValue(structureFixture(true))
    vi.mocked(api.confirmStructure).mockResolvedValue(structureFixture(true))
    vi.mocked(api.triggerGeneration).mockResolvedValue({ generation_id: 'g1', task_id: 't2' })
    vi.mocked(api.getGeneration).mockResolvedValue(generationFixture())
    vi.mocked(api.getGenerationReport).mockResolvedValue({ published: true, quality_status: 'passed' })
  })
  afterEach(() => vi.useRealTimers())

  it('保存完整结构后自动提交确定性生成', async () => {
    const store = useProjectStore()
    store.projectId = 'p1'; store.structure = structureFixture()
    await store.confirm()
    expect(api.confirmStructure).toHaveBeenCalledWith(structureFixture())
    expect(api.triggerGeneration).toHaveBeenCalledWith('p1', 1, [])
    expect(vi.mocked(api.confirmStructure).mock.invocationCallOrder[0])
      .toBeLessThan(vi.mocked(api.triggerGeneration).mock.invocationCallOrder[0]!)
    expect(store.generationPublishable).toBe(true)
    expect(store.stage).toBe('delivered')
  })
  it.each(['success', 'degraded', 'failed', 'running'] as const)('交付判断同时检查可信状态：%s', (status) => {
    const store = useProjectStore()
    store.generation = generationFixture({ status, trusted: false })
    expect(store.generationPublishable).toBe(false)
    store.generation.trusted = true
    expect(store.generationPublishable).toBe(['success', 'degraded'].includes(status))
  })
  it('提交失败保留已确认结构，允许直接重试', async () => {
    const store = useProjectStore()
    store.projectId = 'p1'; store.structure = structureFixture()
    vi.mocked(api.triggerGeneration).mockRejectedValueOnce(new Error('提交失败'))
    await store.confirm()
    expect(store.structure?.confirmed).toBe(true)
    expect(store.stage).toBe('confirmed')
    await store.generate()
    expect(api.confirmStructure).toHaveBeenCalledTimes(1)
    expect(store.generationPublishable).toBe(true)
  })
  it('报告查询失败不污染可信终态，可独立重新读取', async () => {
    const store = useProjectStore()
    store.projectId = 'p1'; store.structure = structureFixture(true)
    vi.mocked(api.getGenerationReport).mockRejectedValueOnce(new Error('报告断线'))
    await store.generate()
    expect(store.stage).toBe('delivered')
    expect(store.generationPublishable).toBe(true)
    expect(store.reportError).toContain('报告暂不可用')
    expect(store.error).toBe('')
    await store.loadGenerationReport()
    expect(store.reportError).toBe('')
    expect(api.triggerGeneration).toHaveBeenCalledTimes(1)
  })
  it('门禁阻断后只有明确操作才授权报告列明的修复', async () => {
    const store = useProjectStore()
    store.projectId = 'p1'; store.structure = structureFixture(true)
    store.generationId = 'g1'; store.stage = 'delivered'
    store.generation = generationFixture({ status: 'failed', trusted: false, review_status: 'not_reviewable' })
    store.generationReport = { published: false, quality_status: 'blocked', repair_request: initialRepairFixture() }
    expect(api.triggerGeneration).not.toHaveBeenCalled()
    await store.authorizeInitialRepair()
    expect(api.triggerGeneration).toHaveBeenCalledWith('p1', 1, ['initial_generation_repair'])
    expect(api.confirmStructure).not.toHaveBeenCalled()
  })
  it.each(['running', 'degraded', 'stale', 'unconfigured'] as const)('无有效待授权失败任务时不能启动修复：%s', async (state) => {
    const store = useProjectStore()
    store.projectId = 'p1'; store.structure = structureFixture(true)
    store.generationId = 'g1'
    store.generation = generationFixture({ status: state === 'running' ? 'running' : state === 'degraded' ? 'degraded' : 'failed',
      trusted: state === 'degraded' })
    const request = initialRepairFixture()
    if (state === 'stale') request.generation_id = 'old'
    if (state === 'unconfigured') request.model.configured = false
    store.generationReport = { published: false, quality_status: 'blocked', repair_request: request }
    await store.authorizeInitialRepair()
    expect(api.triggerGeneration).not.toHaveBeenCalled()
  })
  it('网络中断保留生成标识，恢复只查询，不重复创建', async () => {
    const store = useProjectStore()
    store.projectId = 'p1'; store.structure = structureFixture(true)
    vi.mocked(api.getGeneration).mockRejectedValueOnce(new Error('网络中断'))
    await store.generate()
    expect(store.generationId).toBe('g1')
    expect(store.stage).toBe('generating')
    expect(store.generationPublishable).toBe(false)
    await store.generate()
    expect(api.triggerGeneration).toHaveBeenCalledTimes(1)
    await store.resumeQueries()
    expect(store.generationPublishable).toBe(true)
    expect(api.triggerGeneration).toHaveBeenCalledTimes(1)
  })
  it('轮询超时仍保留非终态，只有恢复查询', async () => {
    vi.useFakeTimers()
    const store = useProjectStore()
    store.projectId = 'p1'; store.structure = structureFixture(true)
    vi.mocked(api.getGeneration).mockResolvedValue(generationFixture({ status: 'running', trusted: false }))
    const pending = store.generate()
    await vi.advanceTimersByTimeAsync(GENERATION_POLL_LIMIT * GENERATION_POLL_INTERVAL_MS)
    await pending
    expect(store.stage).toBe('generating')
    expect(store.generationId).toBe('g1')
    expect(store.busy).toBe(false)
    expect(store.error).toContain('恢复')
    expect(api.triggerGeneration).toHaveBeenCalledTimes(1)
  })
  it('新上传先清除旧项目、任务、结构和恢复数据，并自动抽取', async () => {
    const store = useProjectStore()
    store.projectId = 'old'; store.taskId = 'old'; store.generationId = 'old'
    store.structure = structureFixture(true); store.generation = generationFixture()
    vi.mocked(api.createProject).mockImplementation(async () => {
      expect(store.projectId).toBe('')
      expect(store.structure).toBeNull()
      expect(store.generationId).toBe('')
      expect(sessionStorage.getItem('scholar.initial-workspace')).toBeNull()
      return { project_id: 'p1', source_sha256: 'sha', template_id: 'ouc-bachelor' }
    })
    vi.mocked(api.triggerExtract).mockResolvedValue({ task_id: 't1', status: 'queued' })
    vi.mocked(api.getTask).mockResolvedValue({ task_id: 't1', status: 'done' })
    vi.mocked(api.getStructure).mockResolvedValue(structureFixture())
    await store.start({ docx: new File(['x'], 'paper.docx'), templateId: 'ouc-bachelor' })
    expect(api.triggerExtract).toHaveBeenCalledWith('p1')
    expect(store.stage).toBe('review')
  })
  it('刷新只恢复现有生成查询', async () => {
    const first = useProjectStore()
    first.projectId = 'p1'; first.structure = structureFixture(true)
    vi.mocked(api.getGeneration).mockRejectedValueOnce(new Error('断线'))
    await first.generate()
    setActivePinia(createPinia())
    const restored = useProjectStore()
    await restored.restoreSession()
    expect(restored.generationId).toBe('g1')
    expect(restored.generationPublishable).toBe(true)
    expect(api.triggerGeneration).toHaveBeenCalledTimes(1)
    expect(api.confirmStructure).not.toHaveBeenCalled()
    expect(api.createProject).not.toHaveBeenCalled()
  })
  it('接受版本使用后端评审结果', async () => {
    const store = useProjectStore()
    store.generation = generationFixture()
    vi.mocked(api.acceptVersion).mockResolvedValue({ project_id: 'p1', version_id: 'g1',
      accepted_version_id: 'g1', previous_accepted_version_id: null, review_status: 'accepted' })
    expect(await store.acceptGenerationForRevision()).toBe('p1')
    expect(store.generation.accepted).toBe(true)
  })
  it('报告仍在读取时可以交付，新上传不会被旧报告覆盖', async () => {
    let finishReport: (value: api.PipelineReport) => void = () => { throw new Error('报告请求未启动') }
    vi.mocked(api.getGenerationReport).mockImplementation(() => new Promise(resolve => { finishReport = resolve }))
    const store = useProjectStore()
    store.projectId = 'p1'; store.structure = structureFixture(true)
    await store.generate()
    expect(store.busy).toBe(false)
    expect(store.reportLoading).toBe(true)
    expect(store.generationPublishable).toBe(true)
    store.resetWorkspace()
    finishReport({ published: true, quality_status: 'passed' })
    await Promise.resolve()
    expect(store.generationReport).toBeNull()
    expect(store.reportLoading).toBe(false)
    expect(sessionStorage.getItem('scholar.initial-workspace')).toBeNull()
  })
  it('刷新失败版本恢复自己的确认结构，查询不重新生成', async () => {
    sessionStorage.setItem('scholar.initial-workspace', JSON.stringify({
      projectId: 'p1', taskId: 't1', generationId: 'g1', stage: 'delivered', templateId: 'ouc-bachelor',
    }))
    vi.mocked(api.getGeneration).mockResolvedValue(generationFixture({ status: 'failed', trusted: false }))
    const store = useProjectStore()
    await store.restoreSession()
    expect(store.structure?.confirmed).toBe(true)
    expect(store.stage).toBe('delivered')
    expect(store.templateId).toBe('ouc-bachelor')
    expect(api.triggerGeneration).not.toHaveBeenCalled()
  })
  it('刷新抽取阶段只读任务和结构，不启动模型审查', async () => {
    sessionStorage.setItem('scholar.initial-workspace', JSON.stringify({
      projectId: 'p1', taskId: 't1', generationId: '', stage: 'extracting',
    }))
    vi.mocked(api.getTask).mockResolvedValue({ task_id: 't1', status: 'done' })
    vi.mocked(api.getStructure).mockResolvedValue(structureFixture())
    const store = useProjectStore()
    await store.restoreSession()
    expect(store.stage).toBe('review')
    expect(api.triggerExtract).not.toHaveBeenCalled()
    expect(api.confirmStructure).not.toHaveBeenCalled()
    expect(api.triggerGeneration).not.toHaveBeenCalled()
  })
  it('刷新期间结构查询中断仍提供恢复入口', async () => {
    sessionStorage.setItem('scholar.initial-workspace', JSON.stringify({
      projectId: 'p1', taskId: 't1', generationId: 'g1', stage: 'delivered',
    }))
    vi.mocked(api.getStructure).mockRejectedValueOnce(new Error('断线'))
    const store = useProjectStore()
    await store.restoreSession()
    expect(store.stage).toBe('generating')
    expect(store.error).toContain('恢复查询')
    await store.resumeQueries()
    expect(store.generationPublishable).toBe(true)
    expect(api.triggerGeneration).not.toHaveBeenCalled()
  })

  it.each([true, false])('统一结构审查保持用户确认边界，模型建议可用：%s', async (hasSuggestion) => {
    const store = useProjectStore()
    const snapshot = structureFixture()
    snapshot.headings.push({ unit_id: 'h2', text: '候选标题', level: 'body', suggested_level: 'section',
      confidence: 0.5, evidence: ['bold'], requires_review: true, review_status: 'pending' })
    const review: api.RecognitionReview = { run_id: 'review', project_id: 'p1', mode: 'recognition_review',
      status: 'waiting_user', state_revision: 1, base_revision: 0, detail: '请确认结构',
      model_status: hasSuggestion ? 'succeeded' : 'not_configured', suggestions: hasSuggestion ? [
        { unit_id: 'h2', suggested_level: 'section', confidence: null, rationale: '基于已有证据', evidence: [] },
      ] : [] }
    vi.mocked(api.createProject).mockResolvedValue({ project_id: 'p1', source_sha256: 'sha', template_id: 'ouc-bachelor' })
    vi.mocked(api.triggerExtract).mockResolvedValue({ task_id: 'extract', status: 'queued' })
    vi.mocked(api.getTask).mockResolvedValue({ task_id: 'extract', status: 'done' })
    vi.mocked(api.getStructure).mockResolvedValue(snapshot)
    vi.mocked(api.startRecognitionReview).mockResolvedValue({ ...review, status: 'running', suggestions: [] })
    vi.mocked(api.getRecognitionReview).mockResolvedValue(review)
    await store.start({ docx: new File(['word'], 'paper.docx'), templateId: 'ouc-bachelor', recognitionReviewAllowed: true })
    expect(store.recognitionReview?.status).toBe('waiting_user')
    expect(api.triggerGeneration).not.toHaveBeenCalled()
    if (!hasSuggestion) {
      expect(store.unresolvedStructure).toBe('请确认剩余标题')
      store.setHeadingLevel('h2', 'section')
    }
    vi.mocked(api.resumeRecognitionReview).mockResolvedValue({ ...review, status: 'ready', structure: structureFixture(true) })
    await store.confirm()
    expect(api.resumeRecognitionReview).toHaveBeenCalledTimes(1)
    expect(api.confirmStructure).not.toHaveBeenCalled()
    expect(api.triggerGeneration).toHaveBeenCalledWith('p1', 1, [])
  })

})
