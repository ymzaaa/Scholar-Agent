import { describe, expect, it } from 'vitest'
import router from './router'

// ##### 页面导航板块 #####

describe('工作区路由', () => {
  it('命名路由和项目参数匹配', () => {
    expect(router.resolve({ name: 'workspace' }).path).toBe('/')
    const route = router.resolve({ name: 'project-review', params: { projectId: 'p1' } })
    expect(route.path).toBe('/projects/p1/review')
    expect(router.resolve(route.path).params.projectId).toBe('p1')
  })
  it('未知地址回到工作区', async () => {
    await router.push('/unknown/address')
    expect(router.currentRoute.value.name).toBe('workspace')
  })
})
