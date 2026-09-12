import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import * as api from '../api'
import { useRevisionStore } from './revision'
import { revisionContextFixture, revisionSessionFixture } from '../revisionFixtures'
import { generationFixture, structureFixture } from '../workspaceFixtures'

vi.mock('../api', async (original) => ({ ...await original<typeof api>(),
  getStructure: vi.fn(), listVersions: vi.fn(), getActiveRevisionSession: vi.fn(),
  getGeneration: vi.fn(), getRevisionContext: vi.fn(), processRevision: vi.fn(), resumeRevision: vi.fn(),
}))

// ##### 修订状态夹具板块 #####

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
  vi.mocked(api.getStructure).mockResolvedValue(structureFixture(true))
  vi.mocked(api.listVersions).mockResolvedValue({ project_id: 'project', accepted_version_id: 'parent', versions: [] })
  vi.mocked(api.getGeneration).mockResolvedValue(generationFixture({ generation_id: 'parent', accepted: true }))
  vi.mocked(api.getRevisionContext).mockResolvedValue(revisionContextFixture())
  vi.mocked(api.getActiveRevisionSession).mockResolvedValue(revisionSessionFixture())
})

// ##### 同一运行恢复与具体授权板块 #####

describe('修订目标状态', () => {
  it('恢复已有草稿只查询，不重新理解或执行', async () => {
    const draft = { goal_id: 'goal', feedback_ids: ['feedback'], kind: 'format' as const,
      target_unit_ids: ['body'], description: '取消首行缩进。', missing_information: [],
      request: '取消首行缩进。', confirmed: false, protections: [] }
    vi.mocked(api.getActiveRevisionSession).mockResolvedValue(revisionSessionFixture({ goal_drafts: [draft] }))
    const store = useRevisionStore()
    await store.load('project')
    expect(store.confirmingGoals).toBe(true)
    expect(store.context).toEqual(revisionContextFixture())
    expect(api.processRevision).not.toHaveBeenCalled()
    expect(api.resumeRevision).not.toHaveBeenCalled()
  })
  it('目标确认带同一修订号，未授权时不请求外部格式模型', async () => {
    const store = useRevisionStore()
    await store.load('project')
    vi.mocked(api.getActiveRevisionSession).mockResolvedValue(revisionSessionFixture({
      status: 'reviewable', current_version_id: 'candidate', current_run_revision: 8,
    }))
    await store.confirmGoals([{ goal_id: 'goal' }], false)
    expect(api.resumeRevision).toHaveBeenCalledWith(expect.objectContaining({ current_run_revision: 2 }),
      { goalDecisions: [{ goal_id: 'goal' }] })
    expect(api.processRevision).not.toHaveBeenCalled()
  })
  it('确认格式任务时只授权该具体任务', async () => {
    const store = useRevisionStore()
    await store.load('project')
    await store.confirmGoals([{ goal_id: 'goal' }], true)
    expect(api.resumeRevision).toHaveBeenCalledWith(expect.objectContaining({ current_run_id: 'run' }),
      { goalDecisions: [{ goal_id: 'goal' }], authorizeTask: 'user_feedback_patch_generation' })
  })
  it('网络中断保留运行，不能自动重建反馈批次', async () => {
    const store = useRevisionStore()
    await store.load('project')
    vi.mocked(api.getActiveRevisionSession).mockRejectedValue(new Error('连接中断'))
    await store.resumeFeedbacks({ authorizeTask: 'feedback_normalization' })
    expect(store.error).toBe('连接中断')
    expect(store.session?.current_run_id).toBe('run')
    expect(api.processRevision).not.toHaveBeenCalled()
    expect(api.resumeRevision).toHaveBeenCalledTimes(1)
  })
  it('未可信结果不能评审', async () => {
    vi.mocked(api.getActiveRevisionSession).mockResolvedValue(revisionSessionFixture({ status: 'reviewable' }))
    vi.mocked(api.getGeneration).mockResolvedValue(generationFixture({ trusted: false }))
    const store = useRevisionStore()
    await store.load('project')
    expect(store.reviewable).toBe(false)
  })
})
