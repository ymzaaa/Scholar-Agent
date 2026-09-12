import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import WorkspaceView from './WorkspaceView.vue'
import { generationFixture, structureFixture } from '../workspaceFixtures'
import { useProjectStore } from '../stores/project'

const navigation = vi.hoisted(() => ({ push: vi.fn() }))

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return { ...actual, getTemplates: vi.fn() }

})
vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return { ...actual, useRouter: () => navigation }
})

const templates: api.TemplateSummary[] = [
  {
    template_id: 'ouc-graduate',
    display_name: '研究生固定模板', school: '测试大学',
    education_level: 'graduate', capabilities: { required_english_translation: 'supported' }, bibtex_supported: true,
  },
  {
    template_id: 'ouc-bachelor',
    display_name: '本科固定模板', school: '测试大学',
    education_level: 'undergraduate', capabilities: {}, bibtex_supported: false,
  },
]

describe('workspace template selection', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    sessionStorage.clear()
    vi.mocked(api.getTemplates).mockResolvedValue({ templates })
  })

  it('从服务端加载两个当前模板并隐藏 BibTeX 入口', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(WorkspaceView, { global: { plugins: [pinia] } })
    await flushPromises()

    const templateSelect = wrapper.findAll('select')[0]
    expect(templateSelect.findAll('option')).toHaveLength(3)
    expect(wrapper.find('input[accept=".bib"]').exists()).toBe(false)
    expect(wrapper.find('input[accept=".json"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('参考文献来源')
    expect(wrapper.text()).not.toContain('自动判断')

    await templateSelect.setValue('ouc-bachelor')
    expect((templateSelect.element as HTMLSelectElement).value).toBe('ouc-bachelor')

    await templateSelect.setValue('ouc-graduate')
    expect((templateSelect.element as HTMLSelectElement).value).toBe('ouc-graduate')
  })

  it('仅集中展示歧义引用并保存用户决定', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useProjectStore()
    store.stage = 'review'
    store.structure = {
      project_id: 'p1', source_sha256: 'abc', schema_version: '1.7.0', revision: 0,
      confirmed: false, headings: [], object_bindings: [], counts: {}, issues: [],
      formula_review: { status: 'passed', blocking: false, valid_formula_count: 0, malformed_formula_count: 0, candidates: [] },
      citation_review: { counts: { needs_review: 1 }, all_candidates_classified: false, decisions: [
        { unit_id: 'u-1', raw: '[1]', numbers: ['1'], decision: 'needs_review', evidence: [], source: {} },
        { unit_id: 'u-2', raw: '[2]', numbers: ['2'], decision: 'body_citation', evidence: [], source: {} },
      ] },
    }
    const wrapper = mount(WorkspaceView, { global: { plugins: [pinia] } })
    await flushPromises()
    expect(wrapper.findAll('select')).toHaveLength(2)
    const selector = wrapper.get('select[aria-label="引用 [1]"]')
    await selector.setValue('body_citation')
    expect(api.citationOverrides(store.structure)).toEqual([{ unit_id: 'u-1', decision: 'body_citation' }])
    await selector.setValue('non_citation')
    expect(api.citationOverrides(store.structure)).toEqual([{ unit_id: 'u-1', decision: 'non_citation' }])
  })

  it('通过候选对象选择解决图表关系并阻止未解决结构确认', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useProjectStore()
    store.stage = 'review'
    store.structure = {
      project_id: 'p1', source_sha256: 'abc', schema_version: '1.7.0', revision: 0,
      confirmed: false, counts: {}, issues: [],
      headings: [{ unit_id: 'h1', text: '第一章 测试', level: 'chapter', suggested_level: 'chapter', confidence: 1, evidence: [], requires_review: false, review_status: 'auto_resolved' }],
      object_bindings: [{ caption_unit_id: 'c1', kind: 'table', number: '表1-1', caption: '数据', status: 'needs_review', object_unit_ids: [], candidates: [
        { object_unit_ids: ['t1'], label: '数据表 1' }, { object_unit_ids: ['t2'], label: '数据表 2' },
      ] }],
      formula_review: { status: 'passed', blocking: false, valid_formula_count: 0, malformed_formula_count: 0, candidates: [] },
      citation_review: { decisions: [], counts: {}, all_candidates_classified: true },
    }
    const wrapper = mount(WorkspaceView, { global: { plugins: [pinia] } })
    await flushPromises()
    const confirm = () => wrapper.findAll('button').find((button) => button.text() === '确认结构并生成第一版 PDF')!
    expect(confirm().attributes('disabled')).toBeDefined()
    expect(wrapper.find('input[aria-label="对象内容单元编号"]').exists()).toBe(false)
    await wrapper.get('select[aria-label="图表对象"]').setValue('t2')
    expect(store.structure.object_bindings[0]?.object_unit_ids).toEqual(['t2'])
    expect(confirm().attributes('disabled')).toBeUndefined()
    store.structure.headings[0]!.requires_review = true
    store.structure.headings[0]!.review_status = 'pending'
    await flushPromises()
    expect(confirm().attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('请确认剩余标题')
  })
  it('高置信度标题与引用默认折叠，展开后修改同一份结构', async () => {
    const pinia = createPinia(); setActivePinia(pinia)
    const store = useProjectStore()
    store.stage = 'review'; store.structure = structureFixture()
    store.structure.citation_review.decisions.push({
      unit_id: 'u1', raw: '[1]', numbers: ['1'], decision: 'body_citation', evidence: [], source: {},
    })
    const wrapper = mount(WorkspaceView, { global: { plugins: [pinia] } })
    await flushPromises()
    expect(wrapper.findAll('details').every(item => item.attributes('open') === undefined)).toBe(true)
    await wrapper.get('select[aria-label="标题 第一章 绪论"]').setValue('section')
    expect(store.structure.headings[0]?.level).toBe('section')
    await wrapper.get('select[aria-label="引用 [1]"]').setValue('non_citation')
    expect(store.structure.citation_review.decisions[0]?.decision).toBe('non_citation')
  })

  it.each(['ouc-graduate', 'ouc-bachelor'])('模板能力说明准确且没有通用模型授权：%s', async (templateId) => {
    const pinia = createPinia(); setActivePinia(pinia)
    const store = useProjectStore()
    store.stage = 'review'; store.structure = structureFixture(); store.templateId = templateId
    const wrapper = mount(WorkspaceView, { global: { plugins: [pinia] } })
    await flushPromises()
    expect(wrapper.text().includes('需要英文目录或题注')).toBe(templateId === 'ouc-graduate')
    expect(wrapper.text()).not.toMatch(/G8|G9|N[0-9]|源文件|chapter|格式规范 Agent/)
    expect(wrapper.find('input[type="checkbox"]').exists()).toBe(false)
  })

  it('可信版本没有重新生成，接受后使用命名路由进入修订页', async () => {
    const pinia = createPinia(); setActivePinia(pinia)
    const store = useProjectStore()
    store.stage = 'delivered'; store.generation = generationFixture()
    vi.spyOn(store, 'acceptGenerationForRevision').mockResolvedValue('p1')
    const wrapper = mount(WorkspaceView, { global: { plugins: [pinia] } })
    await flushPromises()
    expect(wrapper.text()).not.toContain('重新生成')
    await wrapper.get('.accept-action').trigger('click')
    await flushPromises()
    expect(navigation.push).toHaveBeenCalledWith({ name: 'project-review', params: { projectId: 'p1' } })
  })

  it('查询中断只恢复查询，失败版本才允许重试', async () => {
    const pinia = createPinia(); setActivePinia(pinia)
    const store = useProjectStore()
    store.stage = 'generating'; store.generationId = 'g1'; store.busy = false
    const resume = vi.spyOn(store, 'resumeQueries').mockResolvedValue()
    const wrapper = mount(WorkspaceView, { global: { plugins: [pinia] } })
    await flushPromises()
    expect(wrapper.text()).not.toContain('结构重试')
    const recover = wrapper.findAll('button').find(item => item.text() === '恢复状态查询')!
    await recover.trigger('click')
    expect(resume).toHaveBeenCalledTimes(1)
    store.stage = 'delivered'; store.generation = generationFixture({ status: 'failed', trusted: false })
    await flushPromises()
    expect(wrapper.text()).toContain('使用已确认结构重试')
    expect(wrapper.find('.accept-action').exists()).toBe(false)
  })
})
