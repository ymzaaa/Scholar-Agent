// ##### 接口数据板块 #####

export type TaskState = 'queued' | 'running' | 'done' | 'failed'
export type GenerationState = 'draft' | 'queued' | 'running' | 'success' | 'degraded' | 'failed' | 'reverted'

export interface TemplateSummary {
  template_id: string
  display_name: string
  school: string
  education_level: string
  capabilities: Record<string, string>
  bibtex_supported: boolean
}

export interface Heading {
  unit_id: string
  text: string
  level: 'chapter' | 'section' | 'subsection' | 'body'
  suggested_level: 'chapter' | 'section' | 'subsection' | 'body'
  confidence: number
  evidence: string[]
  requires_review: boolean
  review_status: 'auto_resolved' | 'pending' | 'resolved'
  overridden?: boolean
  agent_suggestion?: RecognitionReviewSuggestion
}

export interface RecognitionReviewSuggestion {
  unit_id: string
  suggested_level: Heading['level']
  confidence: number | null
  rationale: string
  evidence: string[]
}

export interface RecognitionReview {
  run_id: string
  project_id: string
  mode: 'recognition_review'
  status: 'running' | 'waiting_user' | 'validating' | 'ready' | 'stopped' | 'failed'
  state_revision: number
  base_revision: number
  suggestions: RecognitionReviewSuggestion[]
  model_status: string
  detail: string
  task_id?: string
  structure?: StructureSnapshot
}

export interface ObjectBinding {
  caption_unit_id: string
  kind: 'figure' | 'table'
  number: string
  caption: string
  object_unit_ids: string[]
  status: string
  candidates?: Array<{ object_unit_ids: string[]; label: string }>
  overridden?: boolean
}

export interface CitationDecision {
  unit_id: string
  raw: string
  numbers: string[]
  decision: 'body_citation' | 'non_citation' | 'reference_label' | 'needs_review'
  evidence: string[]
  source: Record<string, unknown>
  context?: string
  overridden?: boolean
}

export interface StructureSnapshot {
  project_id: string
  source_sha256: string
  schema_version: string
  revision: number
  confirmed: boolean
  headings: Heading[]
  object_bindings: ObjectBinding[]
  citation_review: {
    decisions: CitationDecision[]
    counts: Record<string, number>
    all_candidates_classified: boolean
  }
  formula_review: {
    status: 'passed' | 'blocked'
    blocking: boolean
    valid_formula_count: number
    malformed_formula_count: number
    candidates: Array<{
      unit_id: string
      raw_text: string
      error_code: string
      message: string
    }>
  }
  counts: Record<string, number>
  issues: Array<{ code: string; message: string; severity: string }>
}

export interface Artifact {
  kind: 'pdf' | 'latex_source' | 'report'
  name: string
  root: 'output' | 'report'
  relative_path: string
  size: number
  sha256: string
}

export interface Generation {
  generation_id: string
  project_id: string
  structure_revision: number
  source_sha256: string
  task_id: string | null
  status: GenerationState
  quality_status: string
  detail: string
  artifacts: Artifact[]
  parent_version_id: string | null
  version_number: number
  change_origin: string
  created_by: string
  trusted: boolean
  review_status: 'not_reviewable' | 'pending' | 'accepted' | 'rejected' | 'superseded'
  accepted: boolean
  rollback_target_id: string | null
  active_constraints: Array<Record<string, unknown>>
  gate_snapshot: Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface VersionReview {
  project_id: string
  version_id: string
  previous_accepted_version_id: string | null
  accepted_version_id: string | null
  review_status: string
}

export interface RevisionFeedback {
  feedback_id: string
  text: string
  selected_unit_ids: string[]
  page_number?: number
  status: 'pending' | 'planned' | 'resolved' | 'needs_user_input' | 'failed'
  detail: string
  target_references: Array<Record<string, unknown>>
}

export interface PaperConstraint {
  constraint_id: string
  project_id: string
  constraint_type: string
  parameters: Record<string, unknown>
  description: string
  scope_type: 'document' | 'chapter' | 'content_unit'
  scope_ref?: string
  candidate_generation_id: string
  status: 'proposed' | 'confirmed' | 'active' | 'revoked' | 'superseded'
  priority: number
  conflict_ids: string[]
  supersedes_id?: string
  verifier: { mode: 'deterministic' | 'human_review'; checker?: string }
}

export interface FeedbackGoal {
  goal_id: string
  feedback_ids: string[]
  kind: 'format' | 'body_replace' | 'clarification'
  target_unit_ids: string[]
  description: string
  missing_information: string[]
  request: string
  confirmed: boolean
  protections: Array<{ constraint_id: string; unit_id: string; description: string }>
}

export interface GoalDecision {
  goal_id: string
  kind?: 'format' | 'body_replace'
  target_unit_ids?: string[]
  description?: string
  replacement?: { old_text: string; new_text: string }
  discard?: boolean
  supersedes_constraint_ids?: string[]
}

export interface RevisionContext {
  targets: Array<{ unit_id: string; label: string; text: string; body_replace_allowed: boolean }>
  model: { provider: string; name: string; configured: boolean }
}

export interface RevisionResume {
  clarifications?: Record<string, string[]>
  goalDecisions?: GoalDecision[]
  authorizeTask?: 'feedback_normalization' | 'user_feedback_patch_generation'
}

export interface RevisionSession {
  session_id: string
  project_id: string
  base_version_id: string
  current_version_id: string | null
  status: 'collecting' | 'queued' | 'running' | 'reviewable' | 'needs_user_input' | 'failed' | 'accepted' | 'discarded'
  state_revision: number
  feedbacks: RevisionFeedback[]
  current_run_id: string | null
  current_run_revision: number | null
  unresolved: Array<{
    feedback_id: string
    text: string
    detail: string
    reason: 'target_ambiguous' | 'tool_or_authorization_missing'
  }>
  current_task_id: string | null
  evaluation_summary: {
    status?: string
    action_count?: number
    results?: Array<{ detail?: string; visible?: number; total?: number; ratio?: number }>
    detail?: string
  }
  constraint_candidates?: PaperConstraint[]
  goal_drafts: FeedbackGoal[]
  goals: FeedbackGoal[]
  detail: string
  created_at: string
  updated_at: string
}

export interface VersionList {
  project_id: string
  accepted_version_id: string | null
  versions: Generation[]
}

export interface GateIssue {
  code?: string
  message?: string
  rule_id?: string
  detail?: string
  category?: string
  [key: string]: unknown
}

export type InitialRepairTask = 'initial_generation_repair' | 'heading_translation' | 'required_caption_translation'

export interface InitialRepairRequest {
  generation_id: string
  model: { provider: string; name: string; configured: boolean }
  tasks: Array<{ task: InitialRepairTask; title: string; data_scope: string; item_count: number }>
  max_calls: number
}

export interface PipelineReport {
  published: boolean
  quality_status: string
  repair_request?: InitialRepairRequest | null
  gate_statuses?: Record<string, 'passed' | 'failed' | 'degraded' | 'not_run' | 'internal_error'>
  stats?: Record<string, number>
  content_fidelity?: { all_passed?: boolean; [key: string]: unknown }
  structural_gate?: { status?: string; passed?: boolean; [key: string]: unknown }
  compile?: { success?: boolean; warnings?: string[]; [key: string]: unknown }
  format_check?: {
    quality_status?: string
    publish_allowed?: boolean
    blockers?: Array<GateIssue | string>
    degradations?: Array<GateIssue | string>
    [key: string]: unknown
  }
  user_report?: {
    delivery: { status: string; published: boolean; title: string; summary: string }
    findings: Array<{
      finding_id: string
      severity: 'block' | 'degrade'
      category: string
      title: string
      message: string
      impact: string
      recommended_action: string
      occurrence_count: number
      evidence?: Array<Record<string, unknown>>
    }>
    template_warnings: Array<{
      rule_id: string
      category: string
      title: string
      message: string
      occurrence_count: number
      evidence?: Array<Record<string, unknown>>
    }>
    agent: {
      status: string
      detail: string
      actions: Array<{
        tool: string
        status: string
        changed_files: string[]
        before_blockers?: number
        after_blockers?: number
        detail?: string
      }>
    }
    coverage_limitations: Array<{
      rule_id: string
      category: string
      title: string
      message: string
    }>
  }
}

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000'


// ##### HTTP 调用板块 #####

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init)
  const body = await response.json()
  if (!response.ok) {
    throw new Error(body.detail ?? body.user_message ?? `请求失败：${response.status}`)
  }
  return body as T
}

export function getTemplates() {
  return request<{ templates: TemplateSummary[] }>('/api/templates')
}

export async function createProject(input: {
  docx: File
  recognitionReviewAllowed?: boolean
  templateId: string
}) {
  const form = new FormData()
  form.append('docx', input.docx)
  form.append('reference_source', 'word_list')
  form.append('template_id', input.templateId)
  form.append(
    'recognition_review_allowed',
    input.recognitionReviewAllowed ? 'true' : 'false',
  )
  return request<{
    project_id: string
    source_sha256: string
    template_id: string
  }>('/api/projects', {
    method: 'POST', body: form,
  })
}

export function triggerExtract(projectId: string) {
  return request<{ task_id: string; status: TaskState }>(
    `/api/projects/${projectId}/extract`, { method: 'POST' },
  )
}

export function getTask(taskId: string) {
  return request<{
    task_id: string
    status: TaskState
    result?: Record<string, unknown>
    user_message?: string
  }>(`/api/tasks/${taskId}`)
}

export function getStructure(projectId: string) {
  return request<StructureSnapshot>(`/api/projects/${projectId}/structure`)
}

export function confirmStructure(snapshot: StructureSnapshot) {
  return request<StructureSnapshot>(`/api/projects/${snapshot.project_id}/structure`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      base_revision: snapshot.revision,
      citation_overrides: citationOverrides(snapshot),
      heading_overrides: snapshot.headings
        .filter((item) => item.overridden || item.review_status === 'resolved')
        .map((item) => ({ unit_id: item.unit_id, level: item.level })),
      object_binding_overrides: snapshot.object_bindings
        .filter((item) => item.overridden)
        .map((item) => ({
          caption_unit_id: item.caption_unit_id,
          object_unit_ids: item.object_unit_ids,
        })),
      confirmed: true,
    }),
  })
}

export function startRecognitionReview(projectId: string) {
  return request<RecognitionReview>(`/api/projects/${projectId}/recognition-review`, {
    method: 'POST',
  })
}

export function getRecognitionReview(runId: string) {
  return request<RecognitionReview>(`/api/recognition-reviews/${runId}`)
}

export function resumeRecognitionReview(
  run: RecognitionReview, decisions: Record<string, Heading['level']>,
  snapshot: StructureSnapshot,
) {
  return request<RecognitionReview>(`/api/recognition-reviews/${run.run_id}/resume`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      expected_state_revision: run.state_revision,
      decisions,
      object_binding_overrides: snapshot.object_bindings.filter((item) => item.overridden).map(({ caption_unit_id, object_unit_ids }) => ({ caption_unit_id, object_unit_ids })),
      citation_overrides: citationOverrides(snapshot),
    }),
  })
}

export function citationOverrides(snapshot: StructureSnapshot) {
  return snapshot.citation_review.decisions
    .filter((item) => item.overridden)
    .map((item) => ({ unit_id: item.unit_id, decision: item.decision }))
}


// ##### 生成与交付板块 #####

export function triggerGeneration(
  projectId: string, structureRevision: number, allowedTasks: InitialRepairTask[] = [],
) {
  return request<{ generation_id: string; task_id: string }>(
    `/api/projects/${projectId}/generate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        structure_revision: structureRevision,
        external_processing_allowed: allowedTasks.length > 0,
        allowed_llm_tasks: allowedTasks,
      }),
    },
  )
}

export function getGeneration(generationId: string) {
  return request<Generation>(`/api/generations/${generationId}`)
}

export function getGenerationReport(generationId: string, name = 'pipeline_log.json') {
  return request<PipelineReport>(
    `/api/generations/${generationId}/reports/${encodeURIComponent(name)}`,
  )
}

export function artifactUrl(
  generationId: string,
  kind: 'pdf' | 'latex_source' | 'report',
  reportName = 'pipeline_log.json',
) {
  if (kind === 'pdf') return `${API_BASE}/api/generations/${generationId}/pdf`
  if (kind === 'latex_source') return `${API_BASE}/api/generations/${generationId}/latex-source`
  return `${API_BASE}/api/generations/${generationId}/reports/${encodeURIComponent(reportName)}`
}


// ##### 版本与交付板块 #####

export function listVersions(projectId: string) {
  return request<VersionList>(`/api/projects/${projectId}/versions`)
}

export function acceptVersion(versionId: string) {
  return request<VersionReview>(`/api/versions/${versionId}/accept`, {
    method: 'POST',
  })
}

// ##### 修订会话板块 #####

export async function getActiveRevisionSession(projectId: string) {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/revision-session`)
  if (response.status === 404) return null
  const body = await response.json()
  if (!response.ok) throw new Error(body.detail ?? `请求失败：${response.status}`)
  return body as RevisionSession
}

export function addRevisionFeedback(projectId: string, input: {
  feedbackText: string
  selectedUnitIds: string[]
  pageNumber?: number
}) {
  return request<RevisionSession>(`/api/projects/${projectId}/revision-session/feedbacks`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      feedback_text: input.feedbackText,
      selected_unit_ids: input.selectedUnitIds,
      page_number: input.pageNumber,
    }),
  })
}

export function removeRevisionFeedback(sessionId: string, feedbackId: string) {
  return request<RevisionSession>(
    `/api/revision-sessions/${sessionId}/feedbacks/${feedbackId}`,
    { method: 'DELETE' },
  )
}

export function getRevisionContext(projectId: string) {
  return request<RevisionContext>(`/api/projects/${projectId}/revision-context`)
}

export function processRevision(sessionId: string, authorizeUnderstanding = false) {
  return request<{ session_id: string; run_id: string; task_id: string; status: 'queued' }>(
    `/api/revision-sessions/${sessionId}/process`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        external_processing_allowed: authorizeUnderstanding,
        allowed_llm_tasks: authorizeUnderstanding ? ['feedback_normalization'] : [],
      }),
    },
  )
}

export function resumeRevision(
  session: RevisionSession,
  input: RevisionResume,
) {
  if (session.current_run_revision === null) {
    throw new Error('缺少可恢复的 Agent 修订号')
  }
  return request<{ session_id: string; run_id: string; task_id: string; status: 'queued' }>(
    `/api/revision-sessions/${session.session_id}/resume`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        expected_run_revision: session.current_run_revision,
        clarifications: input.clarifications ?? {},
        goal_decisions: input.goalDecisions ?? [],
        external_processing_allowed: input.authorizeTask !== undefined,
        allowed_llm_tasks: input.authorizeTask ? [input.authorizeTask] : [],
      }),
    },
  )
}

export function acceptRevision(sessionId: string) {
  return request<RevisionSession>(`/api/revision-sessions/${sessionId}/accept`, { method: 'POST' })
}

export function discardRevision(sessionId: string) {
  return request<RevisionSession>(`/api/revision-sessions/${sessionId}/discard`, { method: 'POST' })
}
