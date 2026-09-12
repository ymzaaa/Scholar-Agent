<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import type { Heading } from '../api'
import GenerationPanel from '../components/GenerationPanel.vue'
import { useProjectStore } from '../stores/project'


// ##### 表单状态板块 #####

const store = useProjectStore()
const router = useRouter()
const docx = ref<File>()
const recognitionReviewAllowed = ref(false)
const selectedTemplateKey = ref('')
const levels: Array<{ value: Heading['level']; label: string }> = [
  { value: 'chapter', label: '章' },
  { value: 'section', label: '节' },
  { value: 'subsection', label: '小节' },
  { value: 'body', label: '正文' },
]
const reviewHeadings = computed(() => (
  store.structure?.headings.filter((item) => item.requires_review) ?? []
))
const autoHeadings = computed(() => (
  store.structure?.headings.filter((item) => !item.requires_review) ?? []
))
const reviewCitations = computed(() => (
  store.structure?.citation_review.decisions.filter(
    (item) => item.decision === 'needs_review' || item.overridden,
  ) ?? []
))
const selectedTemplate = computed(() => store.templates.find(
  (item) => item.template_id === selectedTemplateKey.value,
))

const autoCitations = computed(() => store.structure?.citation_review.decisions.filter(
  item => !item.overridden && ['body_citation', 'non_citation'].includes(item.decision),
) ?? [])
const requiresEnglish = computed(() => selectedTemplate.value?.capabilities.required_english_translation === 'supported')
const canRetry = computed(() => ['failed', 'reverted'].includes(store.generation?.status ?? ''))

onMounted(async () => {
  await store.loadTemplates()
  await store.restoreSession()
  if (store.templateId) selectedTemplateKey.value = store.templateId
  if (store.generationPublishable && store.generation?.accepted) await acceptForRevision()
})

function selectFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  docx.value = file
}

function submit() {
  if (!docx.value || !selectedTemplate.value) return
  store.start({
    docx: docx.value,
    recognitionReviewAllowed: recognitionReviewAllowed.value,
    templateId: selectedTemplate.value.template_id,
  })
}

function resetUpload() {
  store.resetWorkspace()
  docx.value = undefined
  recognitionReviewAllowed.value = false
  selectedTemplateKey.value = ''
}

async function acceptForRevision() {
  const projectId = await store.acceptGenerationForRevision()
  if (projectId) await router.push({ name: 'project-review', params: { projectId } })
}
</script>

<template>
  <main class="shell">
    <header class="masthead">
      <div class="brand-mark">S</div>
      <div>
        <p class="eyebrow">SCHOLAR AGENT</p>
        <h1>论文转换与校验工作台</h1>
      </div>
      <div class="status-pill" :data-stage="store.stage">
        <span />{{ store.progressLabel }}
      </div>
      <button
        v-if="store.stage !== 'upload'"
        class="secondary-action"
        :disabled="store.busy"
        @click="resetUpload"
      >重新上传 Word</button>
    </header>

    <p v-if="store.sessionNotice" role="status">{{ store.sessionNotice }}</p>
    <section v-if="store.stage === 'upload'" class="panel upload-panel">
      <div class="intro">
        <p class="step">导入</p>
        <h2>先把 Word 变成可核对的结构</h2>
        <p>本步骤只读取论文，不改写正文。图片与表格直接从 Word 提取；生成时使用所选的当前固定模板。</p>
      </div>

      <form class="upload-form" @submit.prevent="submit">
        <label class="select-field">
          <span>论文模板 <b>必填</b></span>
          <select v-model="selectedTemplateKey" required :disabled="store.templatesLoading">
            <option value="" disabled>
              {{ store.templatesLoading ? '正在加载模板…' : '请选择已注册模板' }}
            </option>
            <option
              v-for="template in store.templates"
              :key="template.template_id"
              :value="template.template_id"
            >{{ template.display_name }}</option>
          </select>
        </label>
        <label class="drop-field primary">
          <span class="field-label">Word 论文 <b>必填</b></span>
          <input type="file" accept=".docx" required @change="selectFile" />
          <strong>{{ docx?.name || '选择 .docx 文件' }}</strong>
          <small>文件仅保存到服务端分配的项目工作区</small>
        </label>
        <label class="select-field">
          <span>Agent 结构审查</span>
          <span>
            <input v-model="recognitionReviewAllowed" type="checkbox" />
            允许将低置信度标题的有限证据发送给已配置模型
          </span>
        </label>
        <p v-if="store.error" class="error" role="alert">{{ store.error }}</p>
        <button class="primary-action" :disabled="!docx || !selectedTemplate || store.busy">
          {{ store.busy ? '正在创建项目…' : '开始识别结构' }}
        </button>
      </form>
    </section>

    <section v-else-if="store.stage === 'extracting'" class="panel working-panel">
      <div class="loader" />
      <p class="step">提取与识别</p>
      <h2>正在读取段落、公式、图片与表格</h2>
      <p>正在读取论文内容，请稍候。查询中断后可以恢复，无需重复上传。</p>
      <p v-if="store.error" class="error" role="alert">{{ store.error }}</p>
      <button v-if="!store.busy" class="secondary-action" @click="store.resumeQueries">恢复状态查询</button>
    </section>

    <section v-else class="review-layout">
      <aside class="panel summary-card">
        <p class="step">结构快照</p>
        <h2>{{ store.stage === 'review' ? '检查识别边界' : '结构快照已锁定' }}</h2>
        <dl v-if="store.structure" class="metrics">
          <div><dt>标题</dt><dd>{{ store.structure.counts.headings }}</dd></div>
          <div><dt>内容单元</dt><dd>{{ store.structure.counts.content_units }}</dd></div>
          <div><dt>对象绑定</dt><dd>{{ store.structure.counts.object_bindings }}</dd></div>
        </dl>
        <p>先执行确定性生成和质量检查；发现需要处理的问题后再说明下一步。</p>
        <p v-if="requiresEnglish">所选研究生模板需要英文目录或题注，相关缺项会在质量报告中列出。</p>
        <div v-if="store.structure?.formula_review.blocking" class="error" role="alert">
          检测到 {{ store.structure.formula_review.malformed_formula_count }} 个异常公式，
          当前版本会保留原文并在生成前阻断。
        </div>
        <button
          v-if="store.stage === 'review'"
          class="primary-action"
          :disabled="store.busy || !!store.unresolvedStructure"
          @click="store.confirm"
        >确认结构并生成第一版 PDF</button>
        <p v-if="store.stage === 'review' && store.unresolvedStructure" role="status">{{ store.unresolvedStructure }}</p>
        <button
          v-else-if="store.stage === 'confirmed'"
          class="primary-action"
          :disabled="store.busy"
          @click="store.generate()"
        >生成第一版 PDF</button>
        <button
          v-else-if="store.stage === 'delivered' && canRetry"
          class="secondary-action"
          :disabled="store.busy"
          @click="store.generate()"
        >使用已确认结构重试</button>
        <button v-if="store.stage === 'generating' && !store.busy"
          class="secondary-action" @click="store.resumeQueries">恢复状态查询</button>
        <p v-if="store.reportError" class="error" role="status">{{ store.reportError }}</p>
        <button v-if="store.reportError" class="secondary-action"
          :disabled="store.reportLoading" @click="store.loadGenerationReport">重新读取质量报告</button>
        <p v-if="store.error" class="error" role="alert">{{ store.error }}</p>
      </aside>

      <GenerationPanel
        v-if="store.stage === 'generating' || store.stage === 'delivered'"
        :generation="store.generation"
        :report="store.generationReport"
        :generating="store.stage === 'generating'"
        :publishable="store.generationPublishable"
        :busy="store.busy"
        @accept-for-revision="acceptForRevision"
        @authorize-repair="store.authorizeInitialRepair"
      />

      <section v-else class="panel structure-card">
        <div v-if="reviewCitations.length" class="binding-section">
          <div class="binding-title"><h3>待确认引用</h3></div>
          <article v-for="candidate in reviewCitations" :key="candidate.unit_id" class="binding-row">
            <div><strong>{{ candidate.raw }}</strong><small>{{ candidate.context }}</small></div>
            <select
              :value="candidate.decision"
              :disabled="store.stage === 'confirmed'"
              :aria-label="`引用 ${candidate.raw}`"
              @change="store.setCitationDecision(candidate.unit_id, ($event.target as HTMLSelectElement).value as 'body_citation' | 'non_citation')"
            >
              <option value="needs_review" disabled>请选择</option>
              <option value="body_citation">正文引用</option>
              <option value="non_citation">普通文字</option>
            </select>
          </article>
        </div>
        <div v-if="store.structure?.formula_review.blocking" class="binding-section">
          <div class="binding-title">
            <div><p class="step">发布阻断</p><h3>异常公式候选</h3></div>
            <small>系统不会自动补全或改写</small>
          </div>
          <article
            v-for="candidate in store.structure.formula_review.candidates"
            :key="candidate.unit_id" class="binding-row"
          >
            <div><strong>{{ candidate.raw_text }}</strong><small>{{ candidate.message }}</small></div>

          </article>
        </div>
        <div v-if="reviewHeadings.length" class="binding-title">
          <div><p class="step">待确认</p><h3>低置信度标题候选</h3></div>
          <button
            class="secondary-action" :disabled="store.stage === 'confirmed'"
            @click="store.keepAllReviewHeadingsAsBody"
          >全部保留为正文</button>
        </div>
        <div class="table-head">
          <span>识别文本</span><span>层级</span><span>置信度</span>
        </div>
        <article v-for="heading in reviewHeadings" :key="heading.unit_id" class="heading-row">
          <div>
            <span class="kind">{{ levels.find(item => item.value === heading.level)?.label }}</span>
            <p>{{ heading.text }}</p>
            <small v-if="heading.agent_suggestion">
              结构建议：
              {{ heading.agent_suggestion.rationale }}
            </small>
          </div>
          <select
            :value="heading.level"
            :disabled="store.stage === 'confirmed'"
            @change="store.setHeadingLevel(heading.unit_id, ($event.target as HTMLSelectElement).value as Heading['level'])"
          >
            <option v-for="level in levels" :key="level.value" :value="level.value">{{ level.label }}</option>
          </select>
          <strong>{{ Math.round(heading.confidence * 100) }}%</strong>
        </article>
        <details v-if="autoHeadings.length">
          <summary>已自动确认 {{ autoHeadings.length }} 个高置信度标题</summary>
          <article v-for="heading in autoHeadings" :key="heading.unit_id" class="heading-row">
            <div><span class="kind">{{ levels.find(item => item.value === heading.level)?.label }}</span><p>{{ heading.text }}</p></div>
            <select :value="heading.level" :disabled="store.stage === 'confirmed'"
              :aria-label="`标题 ${heading.text}`"
              @change="store.setHeadingLevel(heading.unit_id, ($event.target as HTMLSelectElement).value as Heading['level'])">
              <option v-for="level in levels" :key="level.value" :value="level.value">{{ level.label }}</option>
            </select>
            <strong>{{ Math.round(heading.confidence * 100) }}%</strong>
          </article>
        </details>
        <details v-if="autoCitations.length" class="binding-section">
          <summary>已自动判断 {{ autoCitations.length }} 个引用候选</summary>
          <article v-for="candidate in autoCitations" :key="candidate.unit_id" class="binding-row">
            <strong>{{ candidate.raw }}</strong>
            <select :value="candidate.decision" :disabled="store.stage === 'confirmed'" :aria-label="`引用 ${candidate.raw}`"
              @change="store.setCitationDecision(candidate.unit_id, ($event.target as HTMLSelectElement).value as 'body_citation' | 'non_citation')">
              <option value="body_citation">正文引用</option><option value="non_citation">普通文字</option>
            </select>
          </article>
        </details>
        <div v-if="!reviewHeadings.length && !autoHeadings.length" class="empty">
          没有识别到标题，请检查 Word 样式或编号。
        </div>

        <div v-if="store.structure?.object_bindings.length" class="binding-section">
          <div class="binding-title">
            <div><p class="step">对象关系</p><h3>图表题注绑定</h3></div>
          </div>
          <article
            v-for="binding in store.structure.object_bindings"
            :key="binding.caption_unit_id"
            class="binding-row"
          >
            <div>
              <strong>{{ binding.number }} · {{ binding.caption }}</strong>
              <small>{{ binding.status === 'bound' ? '已绑定' : '需要确认对象' }}</small>
            </div>
            <select
              v-if="binding.candidates?.length"
              :value="binding.object_unit_ids.join(',')"
              :disabled="store.stage === 'confirmed'"
              aria-label="图表对象"
              @change="store.setObjectBinding(binding.caption_unit_id, ($event.target as HTMLSelectElement).value)"
            >
              <option value="" disabled>请选择对应对象</option>
              <option v-for="option in binding.candidates" :key="option.object_unit_ids.join(',')" :value="option.object_unit_ids.join(',')">{{ option.label }}</option>
            </select>
            <small v-else-if="binding.status !== 'bound'">未找到相邻对象，请检查 Word 中题注与图表的位置后重新上传。</small>

          </article>
        </div>
      </section>
    </section>
  </main>
</template>
