import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as api from '../api'


// ##### 状态模型板块 #####

const RUNNING_STATES = new Set(['queued', 'running'])
const MAX_SESSION_POLLS = 900
const SESSION_POLL_INTERVAL_MS = 1000

export const useRevisionStore = defineStore('revision', () => {
  const projectId = ref('')
  const structure = ref<api.StructureSnapshot | null>(null)
  const session = ref<api.RevisionSession | null>(null)
  const context = ref<api.RevisionContext | null>(null)
  const versions = ref<api.VersionList | null>(null)
  const currentVersion = ref<api.Generation | null>(null)
  const busy = ref(false)
  const error = ref('')
  const versionState = ref<'loading' | 'ready' | 'missing' | 'error'>('loading')

  const pendingCount = computed(() => session.value?.feedbacks.filter(
    (item) => item.status === 'pending',
  ).length ?? 0)
  const processing = computed(() => RUNNING_STATES.has(session.value?.status ?? ''))
  const needsInput = computed(() => session.value?.status === 'needs_user_input')
  const confirmingGoals = computed(() => needsInput.value
    && (session.value?.goal_drafts.length ?? 0) > 0 && session.value?.goals.length === 0)
  const reviewable = computed(() => (
    session.value?.status === 'reviewable'
    && currentVersion.value?.trusted === true
    && ['success', 'degraded'].includes(currentVersion.value.status)
  ))


  // ##### 数据恢复板块 #####

  async function load(id: string) {
    projectId.value = id
    busy.value = true
    error.value = ''
    versionState.value = 'loading'
    try {
      const [snapshot, versionList, active, revisionContext] = await Promise.all([
        api.getStructure(id), api.listVersions(id), api.getActiveRevisionSession(id),
        api.getRevisionContext(id),
      ])
      structure.value = snapshot
      versions.value = versionList
      session.value = active
      context.value = revisionContext
      await loadCurrentVersion()
      if (processing.value) await pollSession()
    } catch (reason) {
      error.value = message(reason, '无法恢复修订工作区。')
      versionState.value = 'error'
    } finally {
      busy.value = false
    }
  }

  async function refresh() {
    const [versionList, active, revisionContext] = await Promise.all([
      api.listVersions(projectId.value), api.getActiveRevisionSession(projectId.value),
      api.getRevisionContext(projectId.value),
    ])
    versions.value = versionList
    if (active) session.value = active
    context.value = revisionContext
    await loadCurrentVersion()
  }

  async function loadCurrentVersion() {
    const sessionVersion = session.value?.status === 'discarded'
      ? undefined : session.value?.current_version_id
    const id = sessionVersion ?? versions.value?.accepted_version_id
    currentVersion.value = id ? await api.getGeneration(id) : null
    versionState.value = currentVersion.value ? 'ready' : 'missing'
  }


  // ##### 反馈操作板块 #####

  async function addFeedback(input: {
    text: string
    selectedUnitIds: string[]
    pageNumber?: number
  }) {
    busy.value = true
    error.value = ''
    try {
      session.value = await api.addRevisionFeedback(projectId.value, {
        feedbackText: input.text,
        selectedUnitIds: input.selectedUnitIds,
        pageNumber: input.pageNumber,
      })
    } catch (reason) {
      error.value = message(reason, '添加反馈失败。')
    } finally {
      busy.value = false
    }
  }

  async function removeFeedback(feedbackId: string) {
    if (!session.value) return
    busy.value = true
    try {
      session.value = await api.removeRevisionFeedback(session.value.session_id, feedbackId)
    } catch (reason) {
      error.value = message(reason, '删除反馈失败。')
    } finally {
      busy.value = false
    }
  }

  async function processFeedbacks(authorizeUnderstanding = false) {
    if (!session.value || pendingCount.value === 0) return
    busy.value = true
    error.value = ''
    try {
      await api.processRevision(session.value.session_id, authorizeUnderstanding)
      session.value = {
        ...session.value, status: 'queued', detail: '反馈批次已进入后台队列。',
      }
      await pollSession()
    } catch (reason) {
      error.value = message(reason, '启动 Agent 失败。')
    } finally {
      busy.value = false
    }
  }

  async function resumeFeedbacks(input: api.RevisionResume) {
    if (!session.value || !needsInput.value) return
    busy.value = true
    error.value = ''
    try {
      await api.resumeRevision(session.value, input)
      session.value = {
        ...session.value, status: 'queued', detail: '补充信息已提交，正在恢复同一 Agent 运行。',
      }
      await pollSession()
    } catch (reason) {
      error.value = message(reason, '恢复 Agent 失败。')
    } finally {
      busy.value = false
    }
  }

  async function pollSession() {
    for (let attempt = 0; attempt < MAX_SESSION_POLLS; attempt += 1) {
      const current = await api.getActiveRevisionSession(projectId.value)
      if (!current) return
      session.value = current
      if (!RUNNING_STATES.has(current.status)) {
        await refresh()
        return
      }
      await delay(SESSION_POLL_INTERVAL_MS)
    }
    throw new Error('修订任务等待超时，请稍后刷新页面。')
  }

  async function confirmGoals(decisions: api.GoalDecision[], authorizeFormat = false) {
    await resumeFeedbacks({ goalDecisions: decisions,
      ...(authorizeFormat ? { authorizeTask: 'user_feedback_patch_generation' as const } : {}) })
  }


  // ##### 版本决策板块 #####

  async function accept() {
    if (!session.value) return
    busy.value = true
    try {
      session.value = await api.acceptRevision(session.value.session_id)
      await refresh()
    } catch (reason) {
      error.value = message(reason, '接受当前版本失败。')
    } finally {
      busy.value = false
    }
  }

  async function discard() {
    if (!session.value) return
    busy.value = true
    try {
      session.value = await api.discardRevision(session.value.session_id)
      await refresh()
    } catch (reason) {
      error.value = message(reason, '放弃修订失败。')
    } finally {
      busy.value = false
    }
  }



  // ##### 公共辅助板块 #####

  function delay(milliseconds: number) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds))
  }

  function message(reason: unknown, fallback: string) {
    return reason instanceof Error ? reason.message : fallback
  }

  return {
    projectId, structure, session, context, versions, currentVersion, busy, error, versionState,
    pendingCount, processing, needsInput, confirmingGoals, reviewable,
    load, addFeedback, removeFeedback, processFeedbacks, resumeFeedbacks,
    accept, discard, confirmGoals,
  }
})
