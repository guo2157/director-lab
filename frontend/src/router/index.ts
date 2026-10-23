import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'home',
      component: () => import('../views/HomeView.vue'),
      meta: { title: '项目' },
    },
  ],
})

router.afterEach((to) => {
  document.title = to.meta.title
    ? `${String(to.meta.title)} · Director Lab`
    : 'Director Lab 漫剧生产工作台'
})

export default router
