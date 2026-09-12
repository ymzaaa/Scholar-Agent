import { mount } from '@vue/test-utils'
import { reactive } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import RevisionView from './RevisionView.vue'
import { revisionContextFixture, revisionSessionFixture } from '../revisionFixtures'
import { generationFixture, structureFixture } from '../workspaceFixtures'

// ##### 修订页面夹具板块 #####

const actions = { load: vi.fn(), addFeedback: vi.fn(), removeFeedback: vi.fn(),
  processFeedbacks: vi.fn(), resumeFeedbacks: vi.fn(), confirmGoals: vi.fn(),
  accept: vi.fn(), discard: vi.fn() }
const state = reactive({
  structure: structureFixture(true), context: revisionContextFixture(),
  session: revisionSessionFixture(), currentVersion: generationFixture(),
  busy: false, error: '', pendingCount: 1, processing: false, reviewable: false,
  confirmingGoals: false, needsInput: true, versionState: 'ready', ...actions,
})
vi.mock('vue-router', () => ({ useRoute: () => ({ params: { projectId: 'project' } }) }))
vi.mock('../stores/revision', () => ({ useRevisionStore: () => state }))

function page() {
  return mount(RevisionView, { global: { stubs: { RouterLink: { template: '<a><slot /></a>' } } } })
}
function draft(kind: 'format' | 'body_replace') {
  state.session = revisionSessionFixture({ goal_drafts: [{ goal_id: 'goal', feedback_ids: ['feedback'],
    kind, target_unit_ids: ['body'], description: '用户明确要求。', missing_information: [],
    request: '用户明确要求。', confirmed: false, protections: [] }] })
  state.confirmingGoals = true
}

// ##### 目标确认与交付行为板块 #####

describe('RevisionView', () => {
  beforeEach(() => {
    Object.values(actions).forEach((action) => action.mockClear())
    Object.assign(state, { context: revisionContextFixture(), session: revisionSessionFixture(),
      currentVersion: generationFixture(), busy: false, reviewable: false, confirmingGoals: false, needsInput: true })
  })
  it('可信候选直接评审，不再逐条二次确认约束', async () => {
    state.reviewable = true
    state.needsInput = false
    state.session = revisionSessionFixture({ status: 'reviewable', current_version_id: 'g1' })
    const wrapper = page()
    expect(actions.load).toHaveBeenCalledWith('project')
    expect(wrapper.find('iframe[title="当前处理版本 PDF"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('待确认的论文级约束')
    await wrapper.get('.accept-version').trigger('click')
    expect(actions.accept).toHaveBeenCalledOnce()
  })
  it('草稿展示具体任务和模型，确认后才授权格式修复', async () => {
    draft('format')
    const wrapper = page()
    expect(wrapper.text()).toContain('核对本批目标')
    expect(wrapper.text()).toContain('test-model')
    expect(wrapper.find('.model-authorization').exists()).toBe(false)
    await wrapper.get('.confirm-goals').trigger('click')
    expect(actions.confirmGoals).toHaveBeenCalledWith([expect.objectContaining({
      goal_id: 'goal', kind: 'format', target_unit_ids: ['body'],
    })], true)
    expect(wrapper.text()).not.toMatch(/user_feedback_patch_generation|waiting_user|G9-|Loop/)
  })
  it('正文替换显示完整原文，提交用户输入的新文本且不授权格式模型', async () => {
    draft('body_replace')
    const wrapper = page()
    expect(wrapper.get('.replacement-original').element.textContent).toContain('原始正文。')
    await wrapper.get('textarea.replacement-text').setValue('用户填写的新正文。')
    await wrapper.get('.confirm-goals').trigger('click')
    expect(actions.confirmGoals).toHaveBeenCalledWith([expect.objectContaining({
      kind: 'body_replace', replacement: { old_text: '原始正文。', new_text: '用户填写的新正文。' },
    })], false)
  })
  it('已有要求默认保留，只有用户勾选才提交替代决定', async () => {
    draft('format')
    state.session.goal_drafts[0]!.protections = [{ constraint_id: 'prior', unit_id: 'body', description: '保留原段距' }]
    const wrapper = page()
    expect(wrapper.text()).toContain('保留原段距')
    const checkbox = wrapper.get('.replace-protection input')
    expect((checkbox.element as HTMLInputElement).checked).toBe(false)
    await wrapper.get('.confirm-goals').trigger('click')
    expect(actions.confirmGoals.mock.calls[0]![0][0].supersedes_constraint_ids).toBeUndefined()
    await checkbox.setValue(true)
    await wrapper.get('.confirm-goals').trigger('click')
    expect(actions.confirmGoals.mock.calls[1]![0][0].supersedes_constraint_ids).toEqual(['prior'])
    expect(wrapper.text()).not.toContain('prior')
  })
  it('模型不可用时禁止格式授权，保留草稿', () => {
    draft('format')
    state.context.model.configured = false
    const wrapper = page()
    expect(wrapper.get('.confirm-goals').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('核对本批目标')
  })
  it('不可信版本不提供 PDF 预览和接受操作', () => {
    state.currentVersion = generationFixture({ trusted: false })
    const wrapper = page()
    expect(wrapper.find('iframe').exists()).toBe(false)
    expect(wrapper.find('.accept-version').exists()).toBe(false)
  })
})
