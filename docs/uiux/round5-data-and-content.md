# 第五轮：数据口径修复与内容优先改造

> 时间：2026-11-19
> 基线：`66a210a`（第四轮结束后）
> 验证方式：实机浏览器 + DOM 几何实测 + 五档视口截图 + 静态门禁
> 截图目录：`docs/uiux/screenshots/round5/`（20 张，1920/1440/1366/768/390 各 4 页）

本轮不是换皮，而是修**真实数据错误**与**内容优先**两件事。前三轮已经把令牌、布局、组件收敛到位，本轮暴露的问题集中在「页面显示的数和数据库里的数对不上」以及「卡片是数据库记录而不是作品」。

---

## 1. P0 级数据缺陷：解析页分镜数三处矛盾

### 现象（实机实测）

`/project/prj_257406918e63/parse` 同屏出现三个互相矛盾的分镜数：

| 位置 | 显示 | 真值 |
|---|---|---|
| 概览统计条 | 26 分镜 | ✅ 正确（5 场景 `shot_count` 合计 7+4+4+5+6） |
| 分组导航「分镜」 | **0** | ❌ |
| 集卡片 | **「3 个分镜」** | ❌ |

### 根因一：查错了 job_type

`ParseView.vue` 的历史解析结果查询写死 `job_type: 'parse'`：

```ts
listJobs({ project_id, job_type: 'parse', page: 1, page_size: 50 })
```

但解析有两条真实链路，落库类型不同：

| 入口 | 执行器 | `job_type` |
|---|---|---|
| `/parse` 页面「解析选中」 | `worker/parse_executor.py` | `parse` |
| 「Agent 智能分析」 | `services/script_agent.py` | `script_agent` |

DB 实测 `prj_257406918e63`：**9 条 `script_agent`、0 条 `parse`**。所以历史结果永远查不到，`parseShots` 恒为空 → 分组导航显示 0。

**修复**：并行查两种类型并按 `created_at` 倒序合并。两者 `result` 结构相同（`scenes[]` / `characters[]` / `props[]`）。

### 根因二：取「最近一次成功」是错的

修完 job_type 后分镜数变成 3，仍与 26 冲突。查 DB 才明白解析支持**分批**：

```
job_3d136de3f7f1  script_agent  2026-11-18 13:44  scenes=1  shots=3   ← 最近，但不完整
job_6de74a09a025  script_agent  2026-11-18 13:37  scenes=5  shots=26  ← 完整
job_5a93cb7052cc  script_agent  2026-11-18 13:37  scenes=5  shots=26
job_bdfc6c370f09  script_agent  2026-11-18 13:13  scenes=5  shots=26
```

最近一次成功任务只是最后一个批次（1 场景 3 镜）。`find(最近一次成功)` 这个策略本身就是错的。

**修复**：改为取「覆盖最完整」的那次，排序键 `分镜数 → 场景数 → 角色数 → 创建时间`：

```ts
function resultWeight(j: Job): number[] {
  const scenes = (j.result?.scenes ?? [])
  const shots = scenes.reduce((n, sc) => n + (sc?.shots?.length ?? 0), 0)
  return [shots, scenes.length, (j.result?.characters?.length ?? 0)]
}
```

### 根因三：集卡片读的字段语义不同源

集卡片显示 `episode_parse_record.asset_count`，该字段确实是「该集分镜数」（`script_agent.py:447` 写入 `per_episode_shots`），但它是**解析那一刻的快照**，不会随后续重新解析更新。实测该记录值是 `3`，而当前解析结果是 `26`。

**修复**：以解析明细 `scenes[].shots[]` 按集聚合为唯一口径，明细取不到时才回退快照值。

### 修复后实测

```
第 1 集 26 个分镜  |  分组导航 分镜 26  |  概览统计 26
台词从 0 → 21（同样是真实数据被 job_type 卡住）
```

### 顺带修的类型缺口

`frontend/src/types/job.ts` 的 `JobType` 缺 `script_agent` / `series_agent`，但 `JobsView.vue` 早就在用这两个值做本地化映射。补上后 `vue-tsc` 才通过。

---

## 2. 分镜看板：卡片是「空卡片」

### 留白

`.shot-card` 写死 `height: 320px`，实际内容只占 254px（缩略图 200 + 信息区 54），**底部固定空白 66px**。26 张卡叠起来是一整片空。

**修复**：`height: auto`，由内容决定。

### 缩略图裁掉构图

`.shot-thumb` 固定 `height: 200px`。9:16 竖屏项目的分镜图被 `object-fit: cover` 裁成横带，人物头脚都看不到。

**修复**：改 `aspect-ratio: var(--shot-thumb-ratio)`，由项目 `aspect_ratio` 注入。

### 卡片过高导致同屏张数太少

改完比例后卡片高 450px，一屏只看得到 5.5 张。9:16 下 220px 宽会把缩略图撑到 391px 高。

**修复**：卡片宽度也随画幅自适应（竖屏 168 / 横屏 320 / 方形 240），整卡稳定在 ~357px。

### 实测数据

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 卡片留白 | 66px | **2px** |
| 卡片高度 | 450px | **357px** |
| 缩略图比例 | 近似方形（裁切） | **0.563 = 精确 9:16** |
| 首行完整可见 | 5 张 | **6 张**（共 7 张） |

---

## 3. 横向滚动：渐隐 mask 是无效方案

7 镜的场景在 1366 宽下只能看到 6 张，没有任何提示，用户会以为场景只有 6 镜。

**第一次尝试（失败）**：`mask-image: linear-gradient(...)` 右侧渐隐。
**失败原因**：近黑背景（`--bg-base: #080809`）下，内容淡出成透明 = 还是黑的，**视觉上完全不可见**。1920 和 1366 两档截图均证实这一点，视觉复核也明确说「没有明显的渐隐遮罩」。

**第二次尝试（有效）**：显式提示按钮「还有更多 →」，可点击，滚到底自动隐藏。

### 实现要点（踩过的坑）

1. **`watch` 必须 `deep: true`**
   `shotsMap` 是「先赋空对象、再逐场景塞 key」的写法，只改属性不改引用。`deep: false` 时 watch 永不触发，按钮一直不出现。

2. **溢出判定要留容差**
   `scrollWidth - scrollLeft - clientWidth > 2`。亚像素取整会让差值出现 1px。

3. **滚动量要 clamp**
   固定滚「3 张卡」实测停在距底 10px 处，按钮不消失（判定条件 >2px）。改为 `Math.min(step, remaining)` 才能精确到底。

### 实测验证

```
1366 宽：row-more 按钮 1 个，仅在溢出 140px 的第 1 行出现
         其余 4 行溢出 0px，无按钮
点击后：scrollLeft 140 / max 140（精确到底），按钮自动隐藏
1920 宽：7 张卡全部放下，溢出 0，无按钮（正确）
390 宽：切 2 列网格，无横向滚动，无按钮（正确）
```

---

## 4. 项目卡片：接入真实封面

### 问题

项目卡片封面是「发光核 + 首字水印 + 网格底纹」的占位，四个作品排在一起完全是「数据库记录卡」。视觉复核原话：「对 AI 创作平台来说，这会显得非常『后台』」。

### 方案

新建 `frontend/src/components/projects/useProjectCovers.ts`：

- 用 `useQueries` 并行取各项目资产，每个项目独立 query key，与资产库页面共用缓存
- 优先级 `keyframe > scene > character`（分镜图最能代表「这部剧的样子」）
- 同类型取版本最新（重新生成后封面跟着变）
- `staleTime: 5min`

**只读现有 `/api/assets?project_id=` 接口，不新增后端通道、不改数据结构。**

### 封面容器比例

第一版用固定 168px 高，实测容器 334×168（2:1 横带），而源图是 229×400（9:16 竖屏），`object-fit: cover` 把人物裁成一条腰带。

**修复**：容器改 `aspect-ratio: 4 / 3` + `object-position: center 22%`（偏上取景保住面部）。

### 占位卡与有图卡必须同高

第一版占位卡 116px、有图卡 251px，同一行卡片高矮不齐，第 1 张卡被拉伸且内容区多出空白。

**修复**：占位态也统一 4:3。

### 关键发现：库里混着 mock 测试图

`prj_ee9942c5fa42` 的「封面」渲染出来是一块深色 + 一行小字。下载原图确认内容是：

```
[MOCK] job 8b2a7b27 out cover
```

**1340 字节**的纯色带边框图（真实分镜缩略图 10–14KB）。

DB 里没有任何可判别字段（`asset` 表无 `size_bytes`、无 mock 标记），前端也拿不到文件体积。只能在图片 `onload` 后用 canvas 判定。

**实测颜色数**（24×24 降采样、每通道量化到 5bit）：

| 图 | 不同颜色数 | 判定 |
|---|---|---|
| mock 占位图 | **19** | 排除 |
| 暗色星云分镜图 | 58 | 保留 |
| 人物夜景分镜图 | 230 | 保留 |

阈值取 **28**：既排除 mock，又给「低细节但真实」的图留余量。canvas 异常时一律按可用处理——宁可显示一张普通图，也不能因检测出错把真实封面隐藏掉。

### 实测结果

| 项目 | 封面 | 说明 |
|---|---|---|
| E2E校验-不完整不落库 | 占位 | 该项目 0 资产，正确回退 |
| E2E自测-1118-1353 | 占位 | **mock 图被检测并排除** |
| rrrr | 真实图 | 星云分镜图 |
| 游乐场 | 真实图 | 摩天轮人物夜景 |

四张卡封面统一 334×251，卡片高度统一 381px。

---

## 5. 解析页重复计数收敛

概览统计条原本 6 项，其中 4 项（场景/角色/道具/分镜/台词）与左侧分组导航的计数**完全重复**，同屏两处相同数字。

**修复**：概览条只保留分组导航没覆盖的两项：

- **集** —— 解析了几个剧本集，不是结果类型
- **待处理** —— 失败集数量，唯一的告警级信息

其余 5 项计数只由分组导航承担（它同时也是筛选入口，数字放在那里更自然）。

实测：概览条从 6 项收敛为「1 集」，分组导航保留 `场景 5 / 角色 2 / 道具 4 / 分镜 26 / 台词 21`。

---

## 6. 工程侧问题

### root 属主再次出现

`docs/uiux/screenshots/`、`docs/plan/` 是 root 属主，导致创建截图目录 `PermissionError`。

```bash
sudo -n chown -R ubuntu:ubuntu docs/uiux docs/plan
```

这是 `page-improvement-plan.md` §7 已记录的同类问题，**第三次复现**。

### playwright 装不上，改用 playwright-core + 系统 Chrome

```
npm i -D playwright  → ECONNRESET（registry 是 mirrors.tencentyun.com）
npm i -D playwright-core --registry=https://registry.npmmirror.com  → 3s 成功
```

系统已有 `/usr/bin/google-chrome`，`playwright-core` 只做协议驱动，`channel: 'chrome'` 直接复用，**零浏览器下载**。

### 颜色门禁连注释也扫

`mask-image` 里写 `#000` 被拦（正确），但把注释改成「而非 #000 字面量」后**仍被拦**——脚本扫的是全文而非 CSS 值。注释里也不能出现 hex。

---

## 7. 验证结论

### 静态门禁

```
npx vue-tsc -b              → 0 error
npm run build               → ✓ 9.83s
npm run check:colors:strict → hex 0 / rgba 0
npm run check:breakpoints   → 全部 1100/768/480/min-769/min-1101
npm run check:contrast      → 全部文本令牌 ≥ 4.5:1
```

### 五档视口截图（20 张，全 PASS）

```
PASS  home        1920x1080  scrollW=1920/1920 clipped=0 cols=4
PASS  home        1440x900   scrollW=1440/1440 clipped=0 cols=4
PASS  home        1366x768   scrollW=1366/1366 clipped=0 cols=4
PASS  home        768x1024   scrollW=768/768   clipped=0 cols=2
PASS  home        390x844    scrollW=390/390   clipped=0 cols=1
PASS  storyboard  全部 5 档   slack=2（卡片留白稳定 2px）
PASS  parse/assets 全部 5 档 零溢出、零裁切
合计 20 张，FAIL 0 张
```

栅格随视口正确降级 4 → 4 → 4 → 2 → 1 列。

### 本轮未触碰

- 任何 API 路径 / 参数 / 响应结构
- 后端代码、数据库、枚举、状态机
- SSE / WebSocket 通道
- Query key 命名（新增的 `['assets','project',id,'cover']` 是独立命名空间）

---

## 8. 遗留项

| 项 | 状态 | 说明 |
|---|---|---|
| `docs/uiux/*.md` 前四份文档的本轮补充 | 🟡 | 本文件为第五轮独立记录，前四份未回填交叉引用 |
| 代码提交 | ⬜ | 全部改动仍在工作区 |
| 其余页面视觉复核 | ⬜ | 本轮只覆盖 首页 / 解析 / 资产库 / 分镜看板 四页 |
| 资产库角色图裁切 | ⬜ | 视觉复核提到「人物占幅偏小、白底对比过硬」，属生成内容问题，未改容器 |
| Naive UI `n-card` 双层 heading / `n-collapse` 键盘不可操作 | ⬜ | 库层面缺陷，`page-improvement-plan.md` §8.3 已记录，需单独决策 |
