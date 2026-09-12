<script setup lang="ts">
import { computed } from 'vue'
import type { Generation, PipelineReport } from '../api'
import { artifactUrl } from '../api'

// ##### 展示模型板块 #####

const props = defineProps<{
  generation: Generation | null
  report: PipelineReport | null
  generating: boolean
  publishable: boolean
  busy: boolean
}>()
const emit = defineEmits<{ acceptForRevision: []; authorizeRepair: [] }>()
const repairRequest = computed(() => {
  const request = props.report?.repair_request
  return !props.publishable && !props.generation?.trusted && props.generation?.status === 'failed'
    && request?.generation_id === props.generation.generation_id && request.tasks.length ? request : null
})
const qualityText = computed(() => props.report?.user_report?.delivery.title
  || (props.publishable ? '第一版 PDF 已准备好' : '当前结果尚未交付'))
const deliveryDetail = computed(() => props.report?.user_report?.delivery.summary
  || '详细检查说明请查看质量报告。')
const findings = computed(() => props.report?.user_report?.findings ?? [])
const templateWarnings = computed(() => props.report?.user_report?.template_warnings ?? [])
const coverageLimitations = computed(() => props.report?.user_report?.coverage_limitations ?? [])
const reportArtifacts = computed(() => props.generation?.artifacts.filter(item => item.kind === 'report') ?? [])
const hasPdf = computed(() => props.generation?.artifacts.some(item => item.kind === 'pdf'))
const hasSource = computed(() => props.generation?.artifacts.some(item => item.kind === 'latex_source'))
const metricItems = computed(() => {
  const stats = props.report?.stats
  return [
    { label: '图片', value: stats?.figures }, { label: '表格', value: stats?.tables },
    { label: '公式', value: stats?.equations }, { label: '引用', value: stats?.citations },
  ].filter(item => typeof item.value === 'number')
})

// ##### 用户说明板块 #####

const gateItems = computed(() => [
  { key: 'content-fidelity', label: '内容保真' }, { key: 'structure', label: '结构检查' },
  { key: 'compile', label: 'PDF 编译' }, { key: 'format', label: '格式检查' },
].map(item => ({ ...item, status: props.report?.gate_statuses?.[item.key] ?? 'not_run' })))
const gateStatusText: Record<string, string> = {
  passed: '通过', failed: '未通过', degraded: '通过（需复核）',
  not_run: '未运行', internal_error: '处理异常',
}
const repairText = computed(() => {
  const agent = props.report?.user_report?.agent
  if (!agent?.actions.length) return ''
  const outcomes = ['正在自动修复', '需要补充信息', '正在复查修复结果', '已修复并复查',
    '未改善，已恢复原结果', '自动修复未完成，已保留原结果', '回滚未完成，请联系维护人员']
  return outcomes.includes(agent.status) ? agent.status : '已进行自动修复，具体结果请查看质量报告。'
})
</script>

<template>
  <section class="panel generation-panel">
    <div v-if="generating" class="generation-working">
      <div class="loader" />
      <p class="step">确定性生成与检查</p>
      <h2>{{ busy ? '正在生成第一版 PDF' : '等待恢复状态查询' }}</h2>
      <p>依次检查内容、结构、编译和格式。结果通过交付校验后提供 PDF。</p>
    </div>
    <template v-else-if="generation">
      <header class="delivery-head" :data-quality="generation.status">
        <div><p class="step">生成结果</p><h2>{{ qualityText }}</h2><p>{{ deliveryDetail }}</p></div>
        <strong>{{ publishable ? '可交付' : '尚未交付' }}</strong>
      </header>
      <div class="delivery-body">
        <dl v-if="metricItems.length" class="generation-metrics">
          <div v-for="item in metricItems" :key="item.label"><dt>{{ item.label }}</dt><dd>{{ item.value }}</dd></div>
        </dl>
        <div v-if="report?.gate_statuses" class="gate-grid" aria-label="生成门禁结果">
          <article v-for="gate in gateItems" :key="gate.key" :data-status="gate.status">
            <strong>{{ gate.label }}</strong><small>{{ gateStatusText[gate.status] || '请查看质量报告' }}</small>
          </article>
        </div>
        <section v-if="repairText" class="repair-summary"><h3>自动修复</h3><p>{{ repairText }}</p></section>
        <section v-if="repairRequest" class="degradation-list repair-consent">
          <h3>本次问题可以请求模型协助</h3>
          <p>模型：{{ repairRequest.model.provider }} · {{ repairRequest.model.name }}</p>
          <ul><li v-for="task in repairRequest.tasks" :key="task.task">
            <strong>{{ task.title }}</strong><p>发送范围：{{ task.data_scope }}</p>
          </li></ul>
          <p>本次最多 {{ repairRequest.max_calls }} 次调用，无隐藏重试。模型结果可能不准确，处理后仍须通过完整检查。</p>
          <p>不授权时可保留报告，修改 Word 后重新上传。</p>
          <button class="primary-action authorize-repair-action" :disabled="busy || !repairRequest.model.configured"
            @click="emit('authorizeRepair')">授权本次处理并重试生成</button>
          <p v-if="!repairRequest.model.configured">模型服务尚未配置，当前不能执行外部处理。</p>
        </section>
        <section v-if="findings.length" class="degradation-list user-findings">
          <h3>需要关注的问题</h3>
          <article v-for="finding in findings" :key="finding.finding_id" class="finding-card">
            <strong>{{ finding.title }}</strong><p>{{ finding.message }}</p>
            <p>影响：{{ finding.impact }}</p><p>建议：{{ finding.recommended_action }}</p>
            <small>检测到 {{ finding.occurrence_count }} 处证据</small>
          </article>
        </section>
        <details v-if="templateWarnings.length" class="degradation-list">
          <summary>模板已有的编译提示</summary>
          <ul><li v-for="item in templateWarnings" :key="item.rule_id">{{ item.title }}：{{ item.occurrence_count }} 项</li></ul>
        </details>
        <details v-if="coverageLimitations.length" class="degradation-list">
          <summary>需要人工复核的范围</summary>
          <ul><li v-for="item in coverageLimitations" :key="item.rule_id">{{ item.title }}</li></ul>
        </details>
        <nav class="artifact-actions" aria-label="生成产物">
          <a v-if="publishable && hasPdf" class="pdf-action"
            :href="artifactUrl(generation.generation_id, 'pdf')" target="_blank" rel="noopener">打开 PDF</a>
          <a v-if="publishable && hasSource" class="source-action"
            :href="artifactUrl(generation.generation_id, 'latex_source')">下载 LaTeX 源码包</a>
          <a v-for="artifact in reportArtifacts" :key="artifact.name" class="report-action"
            :href="artifactUrl(generation.generation_id, 'report', artifact.name)">下载{{ artifact.name === 'pipeline_log.json' ? '质量' : '诊断' }}报告</a>
          <button v-if="publishable && (generation.review_status === 'pending' || generation.accepted)"
            class="primary-action accept-action" :disabled="busy" @click="emit('acceptForRevision')">
            {{ generation.accepted ? '进入修订工作区' : '接受当前版本并进入修订' }}
          </button>
        </nav>
      </div>
      <iframe v-if="publishable && hasPdf" class="pdf-preview"
        :src="artifactUrl(generation.generation_id, 'pdf')" title="生成论文 PDF 预览" />
      <div v-else-if="!publishable" class="blocked-delivery">
        <strong>未发布 PDF</strong><p>当前版本尚不能交付，请查看质量报告了解原因和建议。</p>
      </div>
    </template>
  </section>
</template>
