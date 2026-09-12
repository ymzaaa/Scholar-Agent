import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import GenerationPanel from './GenerationPanel.vue'
import { generationFixture, initialRepairFixture } from '../workspaceFixtures'
import type { PipelineReport } from '../api'

// ##### 交付权限板块 #####

describe('GenerationPanel', () => {
  it('失败任务展示具体模型、发送范围及调用上限，操作不暴露内部任务码', async () => {
    const wrapper = mount(GenerationPanel, { props: {
      generation: generationFixture({ status: 'failed', trusted: false }),
      report: { published: false, quality_status: 'blocked', repair_request: initialRepairFixture() },
      generating: false, publishable: false, busy: false,
    } })
    expect(wrapper.text()).toContain('test-model')
    expect(wrapper.text()).toContain('局部编译诊断')
    expect(wrapper.text()).toContain('3')
    expect(wrapper.text()).not.toContain('initial_generation_repair')
    expect(wrapper.find('input[type="checkbox"]').exists()).toBe(false)
    await wrapper.get('.authorize-repair-action').trigger('click')
    expect(wrapper.emitted('authorizeRepair')).toHaveLength(1)
    await wrapper.setProps({ busy: true })
    expect(wrapper.get('.authorize-repair-action').attributes('disabled')).toBeDefined()
    await wrapper.setProps({ generation: generationFixture(), publishable: true, busy: false })
    expect(wrapper.find('.authorize-repair-action').exists()).toBe(false)
  })
  it('可信版本不依赖报告，可预览、下载和接受；忙碌时不可重复接受', async () => {
    const wrapper = mount(GenerationPanel, { props: {
      generation: generationFixture(), report: null, generating: false, publishable: true, busy: true,
    } })
    expect(wrapper.find('iframe').exists()).toBe(true)
    expect(wrapper.get('.pdf-action').attributes('rel')).toBe('noopener')
    expect(wrapper.get('.source-action').attributes('href')).toContain('/latex-source')
    expect(wrapper.get('.accept-action').attributes('disabled')).toBeDefined()
    expect(wrapper.find('.generation-metrics').exists()).toBe(false)
    await wrapper.setProps({ busy: false })
    await wrapper.get('.accept-action').trigger('click')
    expect(wrapper.emitted('acceptForRevision')).toHaveLength(1)
  })
  it.each(['success', 'degraded', 'failed', 'reverted'] as const)('不可交付 %s 仅提供已登记报告', (status) => {
    const wrapper = mount(GenerationPanel, { props: {
      generation: generationFixture({ status, trusted: false }), report: null,
      generating: false, publishable: false, busy: false,
    } })
    expect(wrapper.find('iframe').exists()).toBe(false)
    expect(wrapper.find('.pdf-action').exists()).toBe(false)
    expect(wrapper.find('.source-action').exists()).toBe(false)
    expect(wrapper.find('.accept-action').exists()).toBe(false)
    expect(wrapper.find('.report-action').exists()).toBe(true)
  })
  it('不存在报告时不伪造下载入口，也不显示原始诊断', () => {
    const wrapper = mount(GenerationPanel, { props: {
      generation: generationFixture({ artifacts: [], detail: 'internal_error C:/private/server' }),
      report: { published: false, quality_status: 'internal_error', format_check: { degradations: [{ code: 'SECRET_TOOL' }] } },
      generating: false, publishable: false, busy: false,
    } })
    expect(wrapper.find('.report-action').exists()).toBe(false)
    expect(wrapper.text()).not.toMatch(/internal_error|SECRET_TOOL|private/)
    expect(wrapper.text()).toContain('质量报告')
  })
  it('优先展示用户报告，统计只展示存在的数值，自动修复不泄露工具状态', () => {
    const report: PipelineReport = {
      published: true, quality_status: 'degraded', stats: { figures: 2 },
      gate_statuses: { 'content-fidelity': 'passed', structure: 'passed', compile: 'passed', format: 'degraded' },
      user_report: {
        delivery: { status: 'degraded', published: true, title: '可以交付，请复核', summary: '请检查长表的分页。' },
        findings: [{ finding_id: 'internal-rule', severity: 'degrade', category: 'format', title: '分页需复核',
          message: '有一张长表。', impact: '可能跨页', recommended_action: '核对 PDF', occurrence_count: 1 }],
        template_warnings: [], coverage_limitations: [],
        agent: { status: '已修复并复查', detail: 'internal_mode', actions: [{ tool: 'secret_tool', status: 'internal_state', changed_files: [] }] },
      },
    }
    const wrapper = mount(GenerationPanel, { props: {
      generation: generationFixture(), report, generating: false, publishable: true, busy: false,
    } })
    expect(wrapper.text()).toContain('可以交付，请复核')
    expect(wrapper.text()).toContain('分页需复核')
    expect(wrapper.findAll('.generation-metrics dd').map(x => x.text())).toEqual(['2'])
    expect(wrapper.text()).not.toMatch(/secret_tool|internal_/)
    expect(wrapper.text()).toContain('已修复并复查')
  })
})
