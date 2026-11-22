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
      path: '/project/:id/assets/impact',
      name: 'asset-impact',
      component: () => import('../views/project/AssetImpactView.vue'),
      meta: { title: '资产变更影响' },
    },
    {
      path: '/project/:id/compose',
      name: 'compose',
      component: () => import('../views/project/ComposeView.vue'),
      meta: { title: '合成与导出' },
    },
    {
      path: '/series',
      name: 'series-list',
      component: () => import('../views/series/SeriesListView.vue'),
      meta: { title: '系列' },
    },
    {
      path: '/series/create',
      name: 'series-create',
      component: () => import('../views/series/SeriesCreateView.vue'),
      meta: { title: '新建系列' },
    },
    {
      path: '/series/:id',
      name: 'series-detail',
      component: () => import('../views/series/SeriesDetailView.vue'),
      meta: { title: '系列详情' },
    },
    {
      path: '/series/:id/continue',
      name: 'series-continue',
      component: () => import('../views/series/SeriesContinueView.vue'),
      meta: { title: '续写剧集' },
    },
    {
      path: '/episodes/:id/review',
      name: 'episode-review',
      component: () => import('../views/series/EpisodeReviewView.vue'),
      meta: { title: '剧集审核' },
    },
    {
      // 任务中心已下线（2026-11-19）：任务进度改在产出页面内联展示。
      // 保留旧路径重定向，避免历史书签/外部链接直接白屏。
      path: '/jobs',
      redirect: { name: 'projects' },
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
