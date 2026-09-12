import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import * as api from '../api'


// ##### 状态模型板块 #####

type WorkspaceStage = 'upload' | 'extracting' | 'review' | 'confirmed' | 'generating' | 'delivered'
export const GENERATION_POLL_LIMIT = 900
export const GENERATION_POLL_INTERVAL_MS = 1000
const EXTRACTION_POLL_LIMIT = 900
const EXTRACTION_POLL_INTERVAL_MS = 700
const SESSION_KEY = 'scholar.initial-workspace'
const TERMINAL_GENERATION_STATES: api.GenerationState[] = [
  'success', 'degraded', 'failed', 'reverted',
]

export const useProjectStore = defineStore('project', () => {
  const projectId = ref('')
  const taskId = ref('')
  const generationId = ref('')
  const stage = ref<WorkspaceStage>('upload')
  const structure = ref<api.StructureSnapshot | null>(null)
  const generation = ref<api.Generation | null>(null)
  const generationReport = ref<api.PipelineReport | null>(null)
  const recognitionReview = ref<api.RecognitionReview | null>(null)
  const templates = ref<api.TemplateSummary[]>([])
  const templatesLoading = ref(false)
  const recognitionReviewAllowed = ref(false)
  const templateId = ref('')
  const reportError = ref('')
  const reportLoading = ref(false)
  const sessionNotice = ref('')
  const error = ref('')
  const busy = ref(false)
  const progressLabel = computed(() => ({
    upload: '等待上传',
    extracting: '正在识别',
    review: '等待确认',
    confirmed: '结构已确认',
    generating: '正在生成',
    delivered: '生成已结束',
  })[stage.value])
  const generationFinished = computed(() => !!generation.value && TERMINAL_GENERATION_STATES.includes(generation.value.status))
  const generationPublishable = computed(() => (
    generation.value?.trusted === true
    && (generation.value.status === 'success' || generation.value.status === 'degraded')
  ))
  const initialRepairRequest = computed(() => {
    const request = generationReport.value?.repair_request
    return generation.value?.status === 'failed' && !generation.value.trusted
      && generation.value.project_id === projectId.value
      && generation.value.structure_revision === structure.value?.revision
      && request?.generation_id === generationId.value && request.tasks.length ? request : null
  })


  // ##### 提取工作流板块 #####

  async function loadTemplates() {
    templatesLoading.value = true
    error.value = ''
    try {
      templates.value = (await api.getTemplates()).templates
      if (!templates.value.length) throw new Error('没有可用的已注册模板。')
    } catch (reason) {
      templates.value = []
      error.value = errorMessage(reason, '模板目录加载失败')
    } finally {
      templatesLoading.value = false
    }
  }

  async function start(input: Parameters<typeof api.createProject>[0]) {
    if (busy.value) return
    resetWorkspace()
    busy.value = true
    templateId.value = input.templateId
    recognitionReviewAllowed.value = Boolean(input.recognitionReviewAllowed)
    try {
      const project = await api.createProject(input)
      projectId.value = project.project_id
      const task = await api.triggerExtract(project.project_id)
      taskId.value = task.task_id
      stage.value = 'extracting'
      await pollExtraction()
    } catch (reason) {
      error.value = errorMessage(reason, '项目创建或提取失败')
      stage.value = taskId.value ? 'extracting' : 'upload'
    } finally {
      busy.value = false
    }
  }

  async function pollExtraction(allowReview = true) {
    for (let attempt = 0; attempt < EXTRACTION_POLL_LIMIT; attempt += 1) {
      const task = await api.getTask(taskId.value)
      if (task.status === 'done') {
        structure.value = await api.getStructure(projectId.value)
        if (
          allowReview && recognitionReviewAllowed.value
          && structure.value.headings.some((item) => item.requires_review)
        ) {
          await startAgentReview()
        }
        stage.value = 'review'
        return
      }
      if (task.status === 'failed') {
        throw new Error(task.user_message ?? '提取任务失败')
      }
      await delay(EXTRACTION_POLL_INTERVAL_MS)
    }
    throw new Error('提取任务等待超时，请检查后端任务状态')
  }

  async function startAgentReview() {
    try {
      recognitionReview.value = await api.startRecognitionReview(projectId.value)
      for (let attempt = 0; attempt < 900; attempt += 1) {
        const current = await api.getRecognitionReview(recognitionReview.value.run_id)
        recognitionReview.value = current
        if (current.status === 'waiting_user' || current.status === 'ready') {
          applyAgentSuggestions(current)
          return
        }
        if (current.status === 'failed' || current.status === 'stopped') throw new Error(current.detail || '结构审查未完成')
        await delay(500)
      }
      throw new Error('Agent 结构审查等待超时')
    } catch (reason) {
      recognitionReview.value = null
      error.value = `${errorMessage(reason, 'Agent 结构审查不可用')}；可继续人工确认。`
    }
  }

  function applyAgentSuggestions(review: api.RecognitionReview) {
    for (const suggestion of review.suggestions) {
      const heading = structure.value?.headings.find(
        (item) => item.unit_id === suggestion.unit_id,
      )
      if (!heading) continue
      heading.agent_suggestion = suggestion
      Object.assign(heading, {
        level: suggestion.suggested_level,
        overridden: true,
        review_status: 'resolved',
      })
    }
  }

  function setHeadingLevel(unitId: string, level: api.Heading['level']) {
    const heading = structure.value?.headings.find((item) => item.unit_id === unitId)
    if (heading) Object.assign(heading, {
      level, overridden: true,
      review_status: heading.requires_review ? 'resolved' : heading.review_status,
    })
  }

  function setCitationDecision(unitId: string, decision: 'body_citation' | 'non_citation') {
    const candidate = structure.value?.citation_review.decisions.find((item) => item.unit_id === unitId)
    if (candidate) Object.assign(candidate, { decision, overridden: true })
  }

  function keepAllReviewHeadingsAsBody() {
    structure.value?.headings
      .filter((item) => item.requires_review && item.review_status === 'pending')
      .forEach((item) => Object.assign(item, {
        level: 'body', overridden: true, review_status: 'resolved',
      }))
  }

  function setObjectBinding(captionUnitId: string, rawIds: string) {
    const binding = structure.value?.object_bindings.find(
      (item) => item.caption_unit_id === captionUnitId,
    )
    if (!binding) return
    binding.object_unit_ids = rawIds.split(',').map((item) => item.trim()).filter(Boolean)
    binding.status = binding.object_unit_ids.length ? 'bound' : 'unbound'
    binding.overridden = true
  }

  const unresolvedStructure = computed(() => {
    const snapshot = structure.value
    if (!snapshot) return '结构尚未提取'
    if (snapshot.headings.some((item) => item.requires_review && item.review_status !== 'resolved')) return '请确认剩余标题'
    if (snapshot.citation_review.decisions.some((item) => item.decision === 'needs_review')) return '请确认剩余引用'
    if (snapshot.object_bindings.some((item) => item.status !== 'bound' || !item.object_unit_ids.length)) return '请解决未绑定的图表关系'
    const objects = snapshot.object_bindings.flatMap((item) => item.object_unit_ids)
    if (new Set(objects).size !== objects.length) return '同一个图表对象不能绑定给多个题注'
    if (!snapshot.headings.some((item) => item.level === 'chapter')) return '至少需要一个章级标题'
    return ''
  })

  async function confirm() {
    if (busy.value || !structure.value) return
    if (unresolvedStructure.value) { error.value = unresolvedStructure.value; return }
    busy.value = true
    error.value = ''
    try {
      if (recognitionReview.value?.status === 'waiting_user') {
        const decisions = Object.fromEntries(
          structure.value.headings
            .filter((item) => item.requires_review)
            .map((item) => [item.unit_id, item.level]),
        )
        recognitionReview.value = await api.resumeRecognitionReview(
          recognitionReview.value, decisions, structure.value,
        )
        if (!recognitionReview.value.structure) {
          throw new Error('Agent 已恢复但未返回确认结构')
        }
        structure.value = recognitionReview.value.structure
      } else {
        structure.value = await api.confirmStructure(structure.value)
      }
      stage.value = 'confirmed'
      busy.value = false
      await generate()
    } catch (reason) {
      error.value = errorMessage(reason, '结构确认失败')
    } finally {
      busy.value = false
    }
  }


  // ##### 生成工作流板块 #####

  async function authorizeInitialRepair() {
    const request = initialRepairRequest.value
    if (!request?.model.configured || busy.value) return
    await generate(request.tasks.map(item => item.task))
  }

  async function generate(allowedTasks: api.InitialRepairTask[] = []) {
    if (busy.value) return
    if (allowedTasks.length && (!initialRepairRequest.value?.model.configured
      || JSON.stringify(allowedTasks) !== JSON.stringify(initialRepairRequest.value.tasks.map(item => item.task)))) {
      error.value = '当前报告没有这些待授权任务，请重新读取质量报告。'
      return
    }
    if (generationId.value && (!generation.value || !TERMINAL_GENERATION_STATES.includes(generation.value.status))) {
      error.value = '已有生成任务，请恢复状态查询。'
      return
    }
    if (generationPublishable.value) return
    if (!structure.value?.confirmed) {
      error.value = '请先确认结构，再生成论文。'
      return
    }
    busy.value = true
    error.value = ''
    generation.value = null
    generationReport.value = null
    reportError.value = ''
    generationId.value = ''
    try {
      const triggered = await api.triggerGeneration(
        projectId.value, structure.value.revision, allowedTasks,
      )
      taskId.value = triggered.task_id
      generationId.value = triggered.generation_id
      stage.value = 'generating'
      await pollGeneration()
    } catch (reason) {
      error.value = errorMessage(reason, '论文生成失败')
      stage.value = generationId.value ? 'generating' : 'confirmed'
    } finally {
      busy.value = false
    }
    if (generationFinished.value) void loadGenerationReport()
  }

  async function pollGeneration() {
    for (let attempt = 0; attempt < GENERATION_POLL_LIMIT; attempt += 1) {
      const current = await api.getGeneration(generationId.value)
      generation.value = current
      if (TERMINAL_GENERATION_STATES.includes(current.status)) {
        stage.value = 'delivered'
        return
      }
      await delay(GENERATION_POLL_INTERVAL_MS)
    }
    throw new Error('等待时间较长，请恢复状态查询；无需重新生成')
  }

  async function acceptGenerationForRevision() {
    if (!generation.value || !generationPublishable.value) {
      throw new Error('当前没有可接受的 PDF 版本。')
    }
    busy.value = true
    error.value = ''
    try {
      if (!generation.value.accepted) {
        const reviewed = await api.acceptVersion(generation.value.generation_id)
        if (reviewed.accepted_version_id !== generation.value.generation_id) {
          throw new Error('接受结果尚未确认，请恢复状态查询。')
        }
        generation.value = { ...generation.value, accepted: true, review_status: 'accepted' }
      }
      return generation.value.project_id
    } catch (reason) {
      error.value = errorMessage(reason, '接受当前版本失败')
      return null
    } finally {
      busy.value = false
    }
  }

  function resetWorkspace() {
    projectId.value = ''
    taskId.value = ''
    generationId.value = ''
    stage.value = 'upload'
    structure.value = null
    generation.value = null
    generationReport.value = null
    recognitionReview.value = null
    recognitionReviewAllowed.value = false
    templateId.value = ''
    reportError.value = ''
    reportLoading.value = false
    sessionNotice.value = ''
    error.value = ''
  }


  // ##### 查询恢复板块 #####

  async function loadGenerationReport() {
    reportError.value = ''
    generationReport.value = null
    if (!generation.value?.artifacts.some((item) => item.kind === 'report' && item.name === 'pipeline_log.json')) return
    const requestedId = generationId.value
    reportLoading.value = true
    try {
      const report = await api.getGenerationReport(requestedId)
      if (generationId.value === requestedId) generationReport.value = report
    } catch {
      if (generationId.value === requestedId) reportError.value = '质量报告暂不可用，可稍后重新读取。'
    } finally {
      if (generationId.value === requestedId) reportLoading.value = false
    }
  }

  async function resumeQueries() {
    if (busy.value) return
    busy.value = true
    error.value = ''
    try {
      if (generationId.value) {
        stage.value = 'generating'
        if (!structure.value) structure.value = await api.getStructure(projectId.value)
        await pollGeneration()
      } else if (taskId.value && stage.value === 'extracting') {
        await pollExtraction(false)
      } else if (projectId.value) {
        structure.value = await api.getStructure(projectId.value)
        stage.value = structure.value.confirmed ? 'confirmed' : 'review'
      }
    } catch {
      error.value = '状态查询暂时中断，请恢复查询。'
    } finally {
      busy.value = false
    }
    if (generationFinished.value) void loadGenerationReport()
  }

  // 只保存查询标识；刷新绝不重放上传、确认或生成请求。
  async function restoreSession() {
    if (projectId.value || stage.value !== 'upload') return
    let saved: unknown
    try {
      const raw = sessionStorage.getItem(SESSION_KEY)
      if (!raw) return
      saved = JSON.parse(raw)
    } catch {
      sessionNotice.value = '无法读取页面恢复信息，请重新上传 Word。'
      return
    }
    if (!saved || typeof saved !== 'object') return
    const record = saved as Record<string, unknown>
    const stages: WorkspaceStage[] = ['upload', 'extracting', 'review', 'confirmed', 'generating', 'delivered']
    if (typeof record.projectId !== 'string' || typeof record.taskId !== 'string'
      || typeof record.generationId !== 'string' || !stages.includes(record.stage as WorkspaceStage)) return
    // 同步赋值完成后再开始查询，持久化监听不会发出网络请求。
    projectId.value = record.projectId
    taskId.value = record.taskId
    generationId.value = record.generationId
    templateId.value = typeof record.templateId === 'string' ? record.templateId : ''
    stage.value = record.stage as WorkspaceStage
    await resumeQueries()
  }

  watch([projectId, taskId, generationId, stage, templateId], () => {
    try {
      if (!projectId.value) sessionStorage.removeItem(SESSION_KEY)
      else sessionStorage.setItem(SESSION_KEY, JSON.stringify({
        projectId: projectId.value, taskId: taskId.value, generationId: generationId.value,
        stage: stage.value, templateId: templateId.value,
      }))
    } catch {
      sessionNotice.value = '浏览器未能保存恢复信息，请保持当前页面打开。'
    }
  }, { flush: 'sync' })

  // ##### 公共辅助板块 #####

  function delay(milliseconds: number) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds))
  }

  function errorMessage(reason: unknown, fallback: string) {
    return reason instanceof Error ? reason.message : fallback
  }

  return {
    projectId, taskId, generationId, stage, structure, generation, generationReport,
    recognitionReview, templates, templatesLoading,
    templateId, reportError, reportLoading, sessionNotice, loadGenerationReport, resumeQueries, restoreSession, error, busy, progressLabel, generationPublishable,
    loadTemplates, start, setHeadingLevel, keepAllReviewHeadingsAsBody,
    unresolvedStructure, setObjectBinding, setCitationDecision, confirm, generate, authorizeInitialRepair, acceptGenerationForRevision, resetWorkspace,
  }
})
