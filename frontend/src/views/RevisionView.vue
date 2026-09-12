<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { artifactUrl } from '../api'
import type { FeedbackGoal, GoalDecision } from '../api'
import { useRevisionStore } from '../stores/revision'

// ##### 页面状态板块 #####

const route = useRoute()
const store = useRevisionStore()
const feedbackText = ref('')
const selectedUnitId = ref('')
const pageNumber = ref<number>()
const clarificationSelections = ref<Record<string, string>>({})
const targets = computed(() => (store.context?.targets ?? []).map((item) => ({
  unitId: item.unit_id, label: item.label,
})))
const deliverable = computed(() => store.currentVersion?.trusted === true
  && ['success', 'degraded'].includes(store.currentVersion.status))
const pdfUrl = computed(() => {
  const id = store.currentVersion?.generation_id
  return id && deliverable.value ? artifactUrl(id, 'pdf') + (pageNumber.value ? '#page=' + pageNumber.value : '') : ''
})
const revisionReady = computed(() => store.versionState === 'ready' && deliverable.value)
const statusText = computed(() => {
  if (store.versionState === 'loading') return '正在读取版本'
  if (store.versionState === 'missing') return '等待接受初始版本'
  if (store.versionState === 'error') return '版本读取失败'
  return ({ collecting: '正在收集反馈', queued: '等待处理', running: '正在处理',
    reviewable: '当前预览可以评审', needs_user_input: '请确认本批目标或补充信息',
    failed: '本轮处理失败', accepted: '当前版本已接受', discarded: '本轮修订已放弃',
  }[store.session?.status ?? 'collecting'])
})
const qualityText = computed(() => store.currentVersion?.quality_status === 'passed' ? '质量检查通过' : '请查看质量报告')
const evaluationDetails = computed(() => {
  const summary = store.session?.evaluation_summary
  return summary?.results?.map((item) => item.detail).filter(Boolean) ?? (summary?.detail ? [summary.detail] : [])
})
onMounted(() => store.load(String(route.params.projectId)))

// ##### 用户目标编辑板块 #####

interface GoalEditor {
  goal: FeedbackGoal
  kind: 'format' | 'body_replace'
  targets: string[]
  description: string
  newText: string
  discard: boolean
  supersedes: string[]
}
const goalEditors = ref<GoalEditor[]>([])
watch(() => (store.session?.goal_drafts ?? []).map((item) => item.goal_id).join(','), () => {
  goalEditors.value = (store.session?.goal_drafts ?? []).map((goal) => ({
    goal, kind: goal.kind === 'body_replace' ? 'body_replace' : 'format',
    targets: [...goal.target_unit_ids], description: goal.description, newText: '', discard: false, supersedes: [],
  }))
}, { immediate: true })
function sourceFor(editor: GoalEditor) {
  return store.context?.targets.find((item) => item.unit_id === editor.targets[0])
}
function protectionsFor(editor: GoalEditor) {
  return editor.goal.protections.filter((item) => editor.targets.includes(item.unit_id))
}
const needsFormatModel = computed(() => goalEditors.value.some((item) => !item.discard && item.kind === 'format'))
const goalsValid = computed(() => goalEditors.value.length > 0 && goalEditors.value.every((item) => item.discard
  || (item.targets.length > 0 && item.description.trim()
    && (item.kind !== 'body_replace' || (item.targets.length === 1
      && sourceFor(item)?.body_replace_allowed && item.newText.trim())))))

// ##### 用户操作板块 #####

async function submitFeedback() {
  const text = feedbackText.value.trim()
  if (!text) return
  await store.addFeedback({ text, selectedUnitIds: selectedUnitId.value ? [selectedUnitId.value] : [], pageNumber: pageNumber.value })
  if (!store.error) { feedbackText.value = ''; selectedUnitId.value = '' }
}
async function processOrResume() {
  if (store.needsInput) {
    const clarifications = Object.fromEntries(Object.entries(clarificationSelections.value)
      .filter(([, unitId]) => Boolean(unitId)).map(([id, unitId]) => [id, [unitId]]))
    await store.resumeFeedbacks({ clarifications, authorizeTask: store.session?.goals.length
      ? 'user_feedback_patch_generation' : 'feedback_normalization' })
    return
  }
  await store.processFeedbacks(true)
}
async function confirmGoals() {
  const decisions: GoalDecision[] = goalEditors.value.map((item) => {
    if (item.discard) return { goal_id: item.goal.goal_id, discard: true }
    const supersedes = protectionsFor(item).filter((prior) => item.supersedes.includes(prior.constraint_id))
      .map((prior) => prior.constraint_id)
    return { goal_id: item.goal.goal_id, kind: item.kind, target_unit_ids: item.targets, description: item.description,
      ...(supersedes.length ? { supersedes_constraint_ids: supersedes } : {}),
      ...(item.kind === 'body_replace' ? { replacement: { old_text: sourceFor(item)?.text ?? '', new_text: item.newText } } : {}) }
  })
  await store.confirmGoals(decisions, needsFormatModel.value)
}
</script>

<template>
  <main class="revision-shell">
    <header class="revision-head">
      <div>
        <RouterLink to="/" class="back-link">← 返回转换工作区</RouterLink>
        <p class="eyebrow">SCHOLAR AGENT</p>
        <h1>论文修订工作台</h1>
      </div>
      <div class="status-pill"><span />{{ statusText }}</div>
    </header>

    <p v-if="store.error" class="error" role="alert">{{ store.error }}</p>

    <section class="revision-grid">
      <article class="panel current-document">
        <header class="current-version-head">
          <div>
            <p class="step">当前处理版本</p>
            <h2>只展示当前预览</h2>
          </div>
          <span v-if="store.currentVersion">
            V{{ store.currentVersion.version_number ?? 1 }} ·
            {{ qualityText }}
          </span>
        </header>
        <iframe
          v-if="pdfUrl"
          class="revision-pdf"
          :src="pdfUrl"
          title="当前处理版本 PDF"
        />
        <div v-else-if="store.versionState === 'loading'" class="revision-empty">
          正在读取当前可用 PDF……
        </div>
        <div v-else-if="store.versionState === 'missing'" class="revision-empty">
          当前项目尚无用户接受版本。请返回转换工作区，接受已生成的 PDF 后再进入修订。
        </div>
        <div v-else class="revision-empty">
          PDF 版本读取失败，请返回转换工作区重试或使用“打开 PDF”。
        </div>
      </article>

      <aside class="panel feedback-panel">
        <section class="feedback-form">
          <p class="step">反馈队列</p>
          <h2>集中说明，再统一处理</h2>
          <label>
            <span>问题对象</span>
            <select v-model="selectedUnitId" :disabled="store.processing || !revisionReady">
              <option value="">暂不选择，由 Agent 请求最少补充信息</option>
              <option
                v-for="target in targets"
                :key="target.unitId"
                :value="target.unitId"
              >{{ target.label }}</option>
            </select>
          </label>
          <label>
            <span>PDF 页码（可选）</span>
            <input
              v-model.number="pageNumber" type="number" min="1"
              :disabled="store.processing || !revisionReady"
            />
          </label>
          <label>
            <span>问题描述</span>
            <textarea
              v-model="feedbackText"
              rows="4"
              maxlength="4000"
              placeholder="例如：该表格显示不完整，请在不改变字号和页边距的前提下处理。"
              :disabled="store.processing || !revisionReady"
            />
          </label>
          <button
            class="secondary-action"
            :disabled="!revisionReady || !feedbackText.trim() || store.busy || store.processing"
            @click="submitFeedback"
          >添加到反馈队列</button>
        </section>

        <section class="feedback-list">
          <article
            v-for="item in store.session?.feedbacks ?? []"
            :key="item.feedback_id"
            class="feedback-item"
            :data-status="item.status"
          >
            <div><strong>{{ item.text }}</strong><small>{{ item.detail }}</small></div>
            <select
              v-if="store.needsInput && !store.confirmingGoals"
              v-model="clarificationSelections[item.feedback_id]"
              :disabled="store.busy || store.processing"
              aria-label="补充反馈目标"
            >
              <option value="">选择具体对象</option>
              <option
                v-for="target in targets"
                :key="target.unitId"
                :value="target.unitId"
              >{{ target.label }}</option>
            </select>
            <button
              v-if="item.status === 'pending'"
              :disabled="store.busy"
              aria-label="删除待处理反馈"
              @click="store.removeFeedback(item.feedback_id)"
            >删除</button>
          </article>
          <div v-if="!store.session?.feedbacks.length" class="feedback-empty">
            尚未添加反馈。可以连续添加多条，再统一生成预览。
          </div>
        </section>

        <section class="revision-actions">
          <p v-if="store.context" class="model-description">
            本批模型：{{ store.context.model.provider }} · {{ store.context.model.name }}
            <span v-if="!store.context.model.configured">（暂不可用）</span>
          </p>
          <section v-if="store.confirmingGoals" class="goal-confirmation">
            <h2>核对本批目标</h2>
            <p>只执行你确认的要求；正文替换必须由你填写完整的新文本。</p>
            <article v-for="editor in goalEditors" :key="editor.goal.goal_id" class="goal-card">
              <strong>{{ editor.goal.request }}</strong>
              <p v-for="missing in editor.goal.missing_information" :key="missing" class="goal-missing">{{ missing }}</p>
              <label>
                <span>处理方式</span>
                <select v-model="editor.kind" :disabled="store.busy || editor.discard">
                  <option value="format">调整格式</option>
                  <option value="body_replace">替换普通正文</option>
                </select>
              </label>
              <label>
                <span>对应内容（正文替换只能选择一个段落）</span>
                <select v-model="editor.targets" multiple :size="Math.min(targets.length || 1, 3)" :disabled="store.busy || editor.discard">
                  <option v-for="target in targets" :key="target.unitId" :value="target.unitId">{{ target.label }}</option>
                </select>
              </label>
              <label>
                <span>确认后的要求</span>
                <textarea v-model="editor.description" rows="2" :disabled="store.busy || editor.discard" />
              </label>
              <template v-if="editor.kind === 'body_replace' && !editor.discard">
                <span>当前完整原文</span>
                <pre class="replacement-original">{{ sourceFor(editor)?.text }}</pre>
                <p v-if="!sourceFor(editor)?.body_replace_allowed" class="goal-missing">请选择只含普通文字的段落。</p>
                <label>
                  <span>你提供的新文本</span>
                  <textarea v-model="editor.newText" class="replacement-text" rows="3" :disabled="store.busy" />
                </label>
              </template>
              <section v-if="protectionsFor(editor).length" class="goal-protections">
                <p>此前要求默认保留。只有本次确实需要替换时才勾选，并在上方写清新的要求。</p>
                <label v-for="prior in protectionsFor(editor)" :key="prior.constraint_id" class="replace-protection">
                  <input v-model="editor.supersedes" :value="prior.constraint_id" type="checkbox" :disabled="store.busy || editor.discard" />
                  <span>用本次要求替代：{{ prior.description }}</span>
                </label>
              </section>
              <label class="discard-goal">
                <input v-model="editor.discard" type="checkbox" :disabled="store.busy" />
                <span>本批不处理此目标</span>
              </label>
            </article>
            <p v-if="needsFormatModel">确认后，仅将这些目标和相关 LaTeX 片段发送给上方模型进行受限格式修复；不会授权改写正文。</p>
            <p v-else>本次执行用户确认的正文替换，不授权外部格式模型。</p>
            <button class="primary-action confirm-goals"
              :disabled="store.busy || !goalsValid || (needsFormatModel && !store.context?.model.configured)"
              @click="confirmGoals"
            >{{ needsFormatModel ? '确认目标并授权本次格式修复' : '确认本批目标' }}</button>
          </section>
          <template v-else-if="!store.reviewable">
            <p v-if="store.session?.goals.length">
              仅对已确认的目标继续受限格式修复；相关片段会发送给上方模型，当前接受版本保持不变。
            </p>
            <p v-else>理解本批反馈只调用一次模型，仅发送反馈及你选择的片段；结果先交你确认，不直接修改论文。</p>
            <button class="primary-action understand-feedback"
              :disabled="!revisionReady || (!store.needsInput && store.pendingCount === 0) || store.busy || store.processing || !store.context?.model.configured"
              @click="processOrResume"
            >{{ store.processing ? '正在处理……' : store.session?.goals.length ? '授权并继续本次格式修复' : '授权理解本批反馈' }}</button>
          </template>
          <p v-if="store.session?.detail">{{ store.session.detail }}</p>
          <div v-if="evaluationDetails.length" class="evaluation-summary">
            <strong>本次检查结果</strong>
            <span v-for="detail in evaluationDetails" :key="detail">{{ detail }}</span>
          </div>
          <div v-if="store.reviewable && deliverable" class="review-actions">
            <button class="primary-action accept-version" :disabled="store.busy" @click="store.accept">接受当前版本</button>
            <button class="secondary-action" :disabled="store.busy" @click="store.discard">放弃本轮修改</button>
          </div>
          <a v-if="pdfUrl" :href="pdfUrl" target="_blank" rel="noopener">在新窗口打开 PDF</a>
        </section>
      </aside>
    </section>
  </main>
</template>
