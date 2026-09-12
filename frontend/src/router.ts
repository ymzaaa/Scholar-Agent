import { createRouter, createWebHistory } from 'vue-router'
import WorkspaceView from './views/WorkspaceView.vue'
import RevisionView from './views/RevisionView.vue'

// ##### 页面路由板块 #####

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'workspace', component: WorkspaceView },
    { path: '/projects/:projectId/review', name: 'project-review', component: RevisionView },
    { path: '/:pathMatch(.*)*', redirect: { name: 'workspace', params: {} } },
  ],
})
