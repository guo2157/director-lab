import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/director',
      name: 'director',
      component: () => import('../views/DirectorConsoleView.vue'),
      meta: { title: '导演台' },
    },
    {
      path: '/',
      name: 'projects',
      component: () => import('../views/ProjectListView.vue'),
      meta: { title: '项目' },
    },
    {
      path: '/project/:id/script',
      name: 'script',
      component: () => import('../views/project/ScriptView.vue'),
      meta: { title: '剧本' },
    },
    {
      path: '/project/:id/estimate',
      name: 'estimate',
      component: () => import('../views/project/EstimateView.vue'),
      meta: { title: '评估' },
    },
    {
      path: '/project/:id/parse',
      name: 'parse',
      component: () => import('../views/project/ParseView.vue'),
      meta: { title: '解析' },
    },
    {
      path: '/project/:id/storyboard-images',
      name: 'storyboard-images',
      component: () => import('../views/project/StoryboardImagesView.vue'),
      meta: { title: '分镜图' },
    },
    {
      path: '/project/:id/storyboard',
      name: 'storyboard',
      component: () => import('../views/project/StoryboardView.vue'),
      meta: { title: '分镜看板' },
    },
    {
      path: '/project/:id/assets',
      name: 'assets',
      component: () => import('../views/project/AssetsView.vue'),
      meta: { title: '资产库' },
    },
    {
      path: '/project/:id/compose',
      name: 'compose',
      component: () => import('../views/project/ComposeView.vue'),
      meta: { title: '合成与导出' },
    },
    {
      path: '/jobs',
      name: 'jobs',
      component: () => import('../views/JobsView.vue'),
      meta: { title: '任务中心' },
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('../views/SettingsView.vue'),
      meta: { title: '设置' },
    },
  ],
})

router.afterEach((to) => {
  document.title = to.meta.title
    ? `${String(to.meta.title)} · Director Lab`
    : 'Director Lab 漫剧生产工作台'
})

export default router
