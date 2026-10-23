# API —— 接口契约（唯一权威源）

> 本文档是 HTTP 接口的唯一权威定义，由原文第 12 章（路由速览）与第 20 章（字段级契约）合并而成。状态机与业务规则见 ARCHITECTURE.md / PRD.md，表结构见 DATABASE.md。

## 1. 通用约定

| 项 | 约定 |
| --- | --- |
| 基础路径 | `/api` |
| 鉴权 | 本期仅内网访问，不启用鉴权；预留 `Authorization: Bearer <token>` |
| 内容类型 | `application/json`；文件上传使用 `multipart/form-data` |
| ID 前缀 | 项目 `prj_`、场景 `scn_`、角色 `chr_`、道具 `prp_`、镜头 `sht_`、资产 `ast_`、任务 `job_`、工作流 `wf_` |
| 时间格式 | ISO 8601 含时区，例如 `2026-10-22T17:00:00+08:00` |
| 成功响应 | 直接返回业务对象或数组，不额外包裹 |
| 失败响应 | 统一结构，含 error.code、error.message、error.detail |
| 分页参数 | `page` 默认 1；`page_size` 默认 20，上限 100 |
| 分页响应 | 含 items、total、page、page_size 四个字段 |
| 排序参数 | `order_by` 配合 `order`（asc/desc）。`order_by` 仅接受各接口明列的白名单字段，不在白名单内返回 400 `invalid_request`；实现层必须用映射表转换为列名，禁止直接拼接请求值 |
| 路由注册顺序 | 同前缀下静态路径必须先于动态路径注册（如 `/api/jobs/stats` 先于 `/api/jobs/{id}`、`/api/characters/merge` 先于 `/api/characters/{id}`），否则静态路径会被 `{id}` 捕获导致 404/422。FastAPI 按注册顺序匹配，实现路由层时必须遵守本约定 |

## 2. 错误码表

| 错误码 | HTTP | 含义 |
| --- | --- | --- |
| invalid_request | 400 | 参数缺失或格式错误 |
| project_not_found | 404 | 项目不存在 |
| scene_not_found | 404 | 场景不存在 |
| shot_not_found | 404 | 镜头不存在 |
| asset_not_found | 404 | 资产不存在 |
| workflow_not_found | 404 | 工作流不存在 |
| job_not_found | 404 | 任务不存在 |
| character_not_found | 404 | 角色不存在 |
| asset_locked | 409 | 资产已锁定，禁止删除或覆盖 |
| asset_in_use | 409 | 资产被镜头引用，需先解绑 |
| workflow_in_use | 409 | 工作流被默认或任务引用，禁止删除 |
| invalid_state_transition | 409 | 状态跃迁非法 |
| script_version_conflict | 409 | 剧本版本与用户确认版本不一致 |
| parse_version_conflict | 409 | 候选解析版本已过期或当前版本已变化 |
| job_submission_unknown | 409 | 远端提交结果未知，需要对账后处理 |
| composition_stale | 409 | 合成版本已失效，需重新合成后再导出 |
| composition_required | 409 | 不存在成功的合成版本，需先合成 |
| workflow_format_invalid | 422 | 非 API 格式或结构不合规 |
| workflow_marker_missing | 422 | 缺少输出标记 |
| workflow_media_missing | 422 | 必填媒体参数未绑定 |
| workflow_capability_mismatch | 422 | 工作流能力声明不支持当前请求 |
| no_matching_workflow | 422 | 无匹配的工作流，需上传或手动指定 |
| h3_duration_unrepresentable | 422 | 目标时长无法在所选 H3 工作流时长/帧数规则内合法表示 |
| h3_reference_required | 422 | H3 参考生视频至少需一张参考图或参考视频，或参考图尺寸/比例不合格 |
| output_missing | 422 | 未产出文件 |
| output_type_mismatch | 422 | 产物类型不符 |
| output_container_mismatch | 422 | 容器格式不符，需改用 h264-mp4 保存节点 |
| duration_mismatch_review_required | 422 | 存在未审核放行的超容差镜头，需审核或重生成 |
| speaker_mapping_required | 422 | 多人对白缺少 speaker 映射，需补全 |
| comfy_node_errors | 422 | 节点校验报错 |
| comfy_execution_error | 422 | 执行过程报错 |
| file_too_large | 413 | 文件超出上限 |
| comfy_unreachable | 502 | ComfyUI 不可达 |
| comfy_upload_failed | 502 | 媒体上传失败 |
| comfy_job_lost | 502 | 远端任务确认丢失 |
| comfy_interrupted | 502 | 任务被中断 |
| comfy_partner_auth_failed | 502 | Partner Node 未登录、额度不足或网络受限，需在 ComfyUI 侧处理 |
| comfyui_test_failed | 502 | 连通性测试失败 |
| too_many_jobs | 429 | 单项目排队任务超过上限（200 个），需先等待或取消部分任务 |
| comfy_timeout | 504 | 轮询超时 |
| database_busy | 503 | 数据库忙，写事务超时，客户端可稍后重试 |

失败分类与界面应对（可重试 vs 需修改指引）的权威分派表见 ARCHITECTURE.md 5.5 节。

## 3. 路由总览

### 项目管理
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /api/projects | 创建项目 |
| GET | /api/projects | 项目列表 |
| GET | /api/projects/{id} | 项目详情 |
| PATCH | /api/projects/{id} | 更新项目 |
| DELETE | /api/projects/{id} | 删除项目 |
| POST | /api/projects/{id}/archive | 归档项目 |
| POST | /api/projects/{id}/unarchive | 取消归档 |

### 剧本与分镜
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /api/projects/{id}/script-versions | 保存剧本并创建不可变版本 |
| POST | /api/projects/{id}/estimate | 评估可制作集数 |
| POST | /api/projects/{id}/estimates/{estimate_id}/confirm | 确认评估与制作范围 |
| POST | /api/projects/{id}/parse | 解析剧本（按选定集范围） |
| POST | /api/projects/{id}/parse-versions/{parse_version_id}/confirm | 确认候选解析版本 |
| GET | /api/projects/{id}/scenes | 场景列表，可选 include_shots |
| POST | /api/scenes | 创建场景 |
| PATCH | /api/scenes/{id} | 更新场景 |
| DELETE | /api/scenes/{id} | 删除场景 |
| POST | /api/scenes/{id}/episodes | 追加场景出现集数 |
| DELETE | /api/scenes/{id}/episodes/{episode} | 移除场景某集出场 |
| GET | /api/scenes/{id}/shots | 场景内镜头列表（按集，分页） |
| GET | /api/shots/{id} | 镜头详情（含一致性、聚合素材） |
| GET | /api/projects/{id}/shots | 按集拉取全部分镜（跨场景扁平列表） |
| POST | /api/shots | 创建镜头 |
| PATCH | /api/shots/{id} | 更新镜头 |
| DELETE | /api/shots/{id} | 删除镜头 |
| PUT | /api/scenes/{id}/shots/order | 镜头排序 |
| GET | /api/projects/{id}/characters | 项目角色列表 |
| POST | /api/characters | 创建角色 |
| POST | /api/characters/merge | 合并重复角色 |
| PATCH | /api/characters/{id} | 更新角色 |
| DELETE | /api/characters/{id} | 删除角色 |
| GET | /api/projects/{id}/props | 项目道具列表 |
| POST | /api/props | 创建道具 |
| PATCH | /api/props/{id} | 更新道具 |
| DELETE | /api/props/{id} | 删除道具 |

### 资产
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /api/assets | 资产列表 |
| POST | /api/assets/upload | 上传资产 |
| GET | /api/assets/{id} | 资产详情 |
| POST | /api/assets/{id}/lock | 锁定资产 |
| POST | /api/assets/{id}/unlock | 解锁资产 |
| POST | /api/assets/{id}/rollback | 回滚版本 |
| DELETE | /api/assets/{id} | 删除资产 |
| POST | /api/assets/batch/delete | 批量删除资产 |
| POST | /api/assets/batch/lock | 批量锁定资产 |
| POST | /api/assets/batch/unlock | 批量解锁资产 |
| POST | /api/assets/export | 打包导出资产 |
| POST | /api/shots/{id}/assets | 绑定资产到镜头 |

### 工作流
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /api/workflows | 工作流列表 |
| POST | /api/workflows | 上传工作流 |
| POST | /api/workflows/{id}/validate | 校验工作流 |
| PATCH | /api/workflows/{id} | 更新工作流 |
| DELETE | /api/workflows/{id} | 删除工作流 |

### 生成
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /api/shots/{id}/generate | 触发单镜生成 |
| POST | /api/shots/{id}/regenerate | 重做单镜 |
| POST | /api/characters/{id}/generate | 生成角色定妆图 |
| POST | /api/props/{id}/generate | 生成道具锚定图 |
| POST | /api/scenes/{id}/generate-baseline | 生成场景基准图 |
| POST | /api/scenes/{id}/generate | 触发场景内镜头批量生成 |
| POST | /api/projects/{id}/generate | 跨场景批量生成 |

### 任务
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /api/jobs | 任务列表 |
| GET | /api/jobs/stats | 任务统计（今日成功/失败、进行中、平均耗时） |
| GET | /api/jobs/{id} | 任务详情 |
| POST | /api/jobs/{id}/retry | 重试任务 |
| POST | /api/jobs/{id}/cancel | 取消任务 |

### 合成与导出
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /api/projects/{id}/compose | 合成可预览成片 |
| GET | /api/projects/{id}/compositions | 合成版本列表 |
| GET | /api/projects/{id}/compositions/{comp_id} | 合成版本详情 |
| POST | /api/projects/{id}/export | 创建独立导出任务 |
| GET | /api/projects/{id}/exports | 导出记录列表 |
| GET | /api/exports/{id}/download | 下载导出文件 |

### 设置与事件流
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /api/settings | 获取设置 |
| PUT | /api/settings | 更新设置 |
| POST | /api/settings/test-comfyui | 测试 ComfyUI 连通性 |
| POST | /api/settings/test-llm | 测试语言模型连通性 |
| POST | /api/settings/backup | 手动触发备份 |
| POST | /api/settings/clear-upload-cache | 清理上传缓存 |
| GET | /api/settings/channels | 获取三方生成通道配置 |
| PUT | /api/settings/channels | 更新三方生成通道配置 |
| POST | /api/settings/channels/{channel_id}/test | 测试三方通道连通性 |
| GET | /api/health | 服务健康状态 |
| GET | /api/events | 实时事件流（SSE） |

---

## 4. 项目管理（字段级契约）

**POST /api/projects**

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| name | string | 是 | 1–100 字符 |
| aspect_ratio | string | 是 | 枚举 9:16 / 16:9 / 1:1 |
| creation_type | string | 否 | 枚举 novel / script，默认 novel |
| description | string | 否 | ≤500 字符 |

响应 201，返回项目对象，初始 `status` 为 `draft`。

**GET /api/projects**：支持 `status`、`keyword`、`page`、`page_size`。

**GET /api/projects/{id}**：返回项目对象，含 `shot_total`、`shot_done`、`current_stage`（派生规则见 ARCHITECTURE.md 第 4 节）。

**PATCH /api/projects/{id}**：可改 `name`、`description`、`settings`。`settings` 可含 `default_image_workflow_id`、`default_video_workflow_id`、`default_tts_workflow_id`、`consistency_pass_threshold`、`consistency_fail_threshold`（须满足 fail 小于 pass，否则返回 400），以及产能参数 `episode_duration_target`（默认 90 秒，软指标）、`shots_per_episode_target`（默认 9）、`shot_duration_default`（默认 10 秒，仅作生成参考）、`max_episodes_per_batch`（单次制作建议上限，默认 10）。

**DELETE /api/projects/{id}**：存在进行中任务时返回 409；成功执行软删除并返回 204。删除后进入延迟清理期，恢复期内可恢复，任务引用的文件不立即物理删除。

**POST /api/projects/{id}/archive**：仅 `active` 可调用，置为 `archived`，返回项目对象。

**POST /api/projects/{id}/unarchive**：仅 `archived` 可调用，置回 `active`，返回项目对象。

## 5. 剧本与分镜

**POST /api/projects/{id}/script-versions**：保存/提交剧本文本并创建不可变版本。请求体 `text` 为 1–50000 字符；成功返回 `script_version_id`、`version`、`content_hash`。后续评估与解析均需引用该版本。

**POST /api/projects/{id}/estimate**：先评估再解析的第一阶段，不产出分镜。

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| script_version_id | string | 是 | 本次评估使用的不可变剧本版本 |
| text | string | 是 | 1–50000 字符；内容哈希必须与 script_version_id 一致 |
| episode_duration_target | number | 否 | 单集目标时长（秒），缺省取项目 settings，默认 90 |

响应直接返回评估对象（非长耗时任务），含 `char_count`、`dialogue_char_estimate`、`estimated_total_duration`、`estimated_episodes`、`episode_breakdown`（每集含 episode、estimated_duration、estimated_shots）、`exceeds_batch_limit`、`suggestion`（超限时提示剧本过长、建议裁剪或分批）。

估算算法：对白语速模型只作为估算输入之一；应结合旁白、动作/纯视觉、停顿和转场的估算时长形成区间，并记录算法/提示词版本及主要假设。字符数/语速计算仅可作为缺少结构信息时的 fallback，不可作为唯一的集数判定依据。评估结果必须持久化到 `script_estimate`；`project.estimated_at` 与 `estimated_text_hash` 仅作进度推导/兼容缓存，不替代评估历史记录。

**POST /api/projects/{id}/estimates/{estimate_id}/confirm**：确认评估结果与制作范围。请求体含 `script_version_id`、用户确认的 `episode_start`、`episode_count` 及可选时长调整；评估版本与选择不匹配时返回 409 `script_version_conflict`。确认后 `script_estimate.user_confirmed=true`。

**POST /api/projects/{id}/parse**

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| script_version_id | string | 是 | 必须指向用户确认的不可变剧本版本 |
| text | string | 是 | 1–50000 字符；必须与 script_version_id 的内容哈希一致 |
| episode_start | int | 否 | 本次解析的起始集数，缺省为 1 |
| episode_count | int | 否 | 本次解析的集数，缺省为已确认评估中的全部集数 |
| replace_existing | bool | 否 | 默认 false。true 时仅在用户确认候选解析差异后替换选定且未锁定的集；不得直接覆盖当前版本 |
| llm_model | string | 否 | 缺省使用设置中的模型 |

调用前必须已执行并确认与 `script_version_id` 匹配的评估，未评估或未确认时返回 400，版本哈希不匹配时返回 409 `script_version_conflict`。解析仅生成 `parse_version.status=candidate` 的候选版本，范围外的集不落库。返回 `job_id`；解析任务通过 SSE `job.finished` 事件通知结果，前端也可轮询 `GET /api/jobs/{job_id}`。用户确认后由确认接口在事务中切换 `project.current_parse_version_id`，并标记旧版本为 superseded。候选解析失败或未确认时当前分镜保持不变。

解析版本切换时的产物处理（按 diff_summary 逐镜头）：新增镜头→无产物、状态 pending；内容变化的镜头→标记 redo、旧产物标 stale；已删除镜头的产物→标记 archived、不物理删除；未变化的镜头→产物保留、状态不变。

覆盖与集级锁定的优先级（两套规则不冲突，锁定为硬约束）：

| 该集 locked | replace_existing | 结果 |
| --- | --- | --- |
| false | false | 跳过该集，已有分镜保留不动 |
| false | true | 覆盖该集的分镜与规划 |
| true | false | 跳过该集 |
| true | true | 仍跳过该集，锁定优先于覆盖开关，并在结果中标注为因锁定而跳过 |

解析请求范围内的集若全部已锁定，任务直接返回 400，提示先解锁，不进入模型调用；部分集锁定时正常解析，结果分别列出已覆盖集与因锁定跳过的集。镜头级人工编辑不设锁定，只能通过集级锁定保护。

解析完成后 `job.finished` 事件的 `result` 字段包含：`project_analysis`（summary/theme/highlights/foreshadowing）、`characters`（name/description/traits/appearance，不产出 voice_id 由审核阶段补充）、`props`（name/description/appearance）、`scenes`（episode/title/description/time_note/shots 数组）、`shots`（完整镜头字段，口径见 DATABASE.md shot 表与 PROMPTS.md）、`episodes`（分集规划数组，整体写入 project.episode_plan，按集覆盖未锁定集）。

**POST /api/projects/{id}/parse-versions/{parse_version_id}/confirm**：确认候选解析版本。请求需携带当前 `current_parse_version_id`（可为空）作为乐观锁版本；期间当前版本已变化则返回 409 `parse_version_conflict` 并要求刷新差异。确认成功后原子切换当前解析版本，保留旧版本及产物引用，并将旧版本关联的运行任务取消或标记过期。

**GET /api/projects/{id}/scenes**：可选 `episode` 按集筛选，可选 `include_shots`（默认 true）。`episode` 筛选依据 scene_episode 关联表（含复用场景）；场景对象含 `episode`（首次出现集数）与 `episodes`（全部出现集数数组）。`include_shots=false` 时只返回场景与 `shot_count`，镜头数超过 100 的项目前端必须使用 `include_shots=false` 分批加载。

**POST /api/scenes**：`project_id`、`title`、`description`、`episode`（可选，默认 1，正整数）、`seq`（可选，缺省追加项目末尾）。创建时自动写入一条 scene_episode 记录。

**POST /api/scenes/{id}/episodes**：追加出现集数，请求体 `episode` 与 `seq_in_episode`。重复添加同一集返回 409。响应对象含 `requires_parse` 布尔值：追加的集数超出当前已解析范围时为 true，提示该集尚无分镜产物、须先追加解析方可进入生成（规则见 PRD.md M2 业务规则）；在已解析范围内时为 false。

**DELETE /api/scenes/{id}/episodes/{episode}**：移除该场景在某集的出场。移除最后一集时拒绝，须改用删除场景接口。该集下已存在镜头时返回 409 `asset_in_use`，需先删除该集镜头；不提供级联删除。

**PATCH /api/scenes/{id}** 与 **DELETE /api/scenes/{id}**：删除级联其下镜头，需前端二次确认。

**POST /api/shots**：`scene_id`、`episode`（必填）、`shot_kind`（必填，枚举 dialogue/narration/visual/transition）、`dialogue`（可选；shot_kind 为 dialogue/narration 时必填）、`image_prompt`、`video_prompt`（可选）、`duration`。约束：`(scene_id, episode)` 必须在 scene_episode 中存在，否则返回 422；该约束由创建接口校验，不依赖数据库外键。

**GET /api/projects/{id}/shots**：按集拉取跨场景扁平镜头列表。`episode` 必填，可选 `page`、`page_size`。排序：先按场景在该集出场顺序（scene_episode.seq_in_episode）升序，同场景内按镜头 seq 升序。每条镜头附带 `scene_title`、`scene_seq` 与 `current_assets`。

**GET /api/shots/{id}**：返回镜头对象，含 `consistency` 字段（`score` 0–1，`verdict` 枚举 passed/review/failed），并返回聚合素材：`scene`（id/seq/title/episode）、`character`（id/name/appearance/portrait_asset_id，无则 null）、`props`（id/name/anchor_asset_id 数组）、`current_assets`（keyframe/clip/audio 三键，各含 asset_id/file_path/thumb_path/version，未生成为 null）。

`current_assets` 取值口径：取依赖版本与当前输入匹配、状态为 current 且来源任务 succeeded 的产物；完成时间仅用于同一有效输入快照下的排序，不能覆盖更新输入对应的有效产物（详见第 8 节多版本任务有效性规则）。

**PATCH /api/shots/{id}**：修改 `image_prompt`、`video_prompt`、`dialogue`、`duration` 或镜头结构字段后，按依赖矩阵标记相关产物 `stale`，不自动触发任务。仅修改 `video_prompt` 时只影响视频，分镜图不受影响。修改 `duration` 时若新值超出 H3 支持范围（4–15 整数秒），返回 warning 字段提示 `duration_out_of_range`，同时仍接受修改并标记相关产物 `stale`。

**GET /api/scenes/{id}/shots**：返回该场景镜头数组，按 seq 升序。`episode` 必填；可选 `page`、`page_size`。同一场景在不同集的镜头相互独立。

**PUT /api/scenes/{id}/shots/order**：请求体 `episode` 与 `shot_ids` 数组。排序仅在同一场景同一集内进行，`shot_ids` 含其他场景或其他集的镜头 ID 时返回 400。

**GET /api/projects/{id}/characters**：返回项目内角色数组。

**POST /api/characters**：`project_id`、`name`、`description`、`traits`、`appearance`、`voice_id`（可选）。`name` 项目内唯一，重复返回 409。

**POST /api/characters/merge**：合并重复角色（剧本解析审核页使用）。请求体：`source_id`（被合并的源角色）、`target_id`（保留的目标角色）。处理规则：① 全部镜头的 `character_id` 与 `dialogue_segments[].speaker_id` 中引用 source_id 的改为 target_id；② 提示词中的 `@源角色名` 同步改写为 `@目标角色名`（同改名规则）；③ 源角色定妆图处置：目标已有定妆图时，源定妆图转为普通 character 类型资产保留不删除，其镜头 asset_ref 引用一并改指目标定妆图；目标尚无定妆图时，源定妆图转为目标角色的定妆图（回写 `target.portrait_asset_id`），既有镜头引用不变；④ 完成后删除源角色记录，返回目标角色对象与受影响镜头数。同一事务内完成；source 与 target 相同返回 400 `invalid_request`；任一角色不存在返回 404 `character_not_found`。

**PATCH /api/characters/{id}**：可改 `name`、`description`、`traits`、`appearance`、`voice_id`。改名时同步改写所有 shot.image_prompt 与 shot.video_prompt 中的 `@旧名` 引用。

**DELETE /api/characters/{id}**：被镜头引用时返回 409 `asset_in_use`，需先解除引用。

**GET /api/projects/{id}/props**：返回项目内道具数组。

**POST /api/props**：`project_id`、`name`、`description`、`appearance`。`name` 项目内唯一，重复返回 409。

**PATCH /api/props/{id}**：可改 `name`、`description`、`appearance`。改名时同步改写所有 shot.image_prompt 与 shot.video_prompt 中的 `@旧名` 引用。

**DELETE /api/props/{id}**：被镜头引用时返回 409 `asset_in_use`，需先解除引用。

## 6. 资产

**GET /api/assets**：`project_id` 必填，可选 `asset_type`、`page`、`page_size`。

**POST /api/assets/upload**：multipart，单文件上限 500MB，成功返回 201 与资产对象。

**GET /api/assets/{id}**：返回资产对象，含 `versions` 数组与 `referenced_by` 镜头列表。

**POST /api/assets/{id}/lock**：锁定后不可删除。无请求体，返回资产对象。

**POST /api/assets/{id}/unlock**：恢复可删除状态。无请求体，返回资产对象。

**POST /api/assets/{id}/rollback**：请求体 `target_version`。回滚生成新版本号，`parent_id` 指向被回滚版本；引用该资产的镜头标记『待重做』，但不自动触发任务。

**DELETE /api/assets/{id}**：被引用返回 409 `asset_in_use`，已锁定返回 409 `asset_locked`。

**POST /api/shots/{id}/assets**：请求体 `asset_id` 与 `role`。手动绑定仅允许 4 种 role：`reference` / `first_frame` / `end_frame` / `audio`；其余 role（identity/costume/scene/prop/prev_frame/dialogue/sfx/bgm）由系统在生成接口内部自动注入，不开放手动绑定。单镜头关联资产总数不得超过 6 个，超限返回 400；同一镜头重复绑定同一资产返回 409 `asset_in_use`。该接口用于手动覆盖自动注入结果；正常生成视频时由后端在 generate 内部自动完成绑定，无需前端调用。

**POST /api/assets/batch/delete**：请求体 `asset_ids` 数组。任一资产被锁定或引用时整批返回 409，不部分执行。

**POST /api/assets/batch/lock** / **POST /api/assets/batch/unlock**：请求体 `asset_ids` 数组。

**POST /api/assets/export**：请求体 `asset_ids` 数组，返回打包下载流。使用 POST 而非 GET，避免资产数量较多时 URL 过长触发网关限制。

## 7. 工作流

**GET /api/workflows**：可选 `workflow_type`，返回列表含 `is_default` 与 `used_by_jobs`。

**POST /api/workflows**：multipart，字段 `name`、`workflow_type`（枚举 image/video/tts/bgm/sfx）、`description`、`trigger`（触发条件 JSON）、`capabilities`（能力声明 JSON；缺失时音频与时长能力视为未知，界面提示）、`file`（API 格式 JSON ≤10MB）。响应返回 `parsed_schema.params` 与 `parsed_schema.outputs`。

**POST /api/workflows/{id}/validate**：仅做静态校验，不访问 ComfyUI，返回 `valid` 与 `issues` 数组。

**PATCH /api/workflows/{id}**：修改 `name`、`is_default` 或定义内容时生成新的不可变工作流版本；设为默认时同类型其他工作流自动取消默认。已排队/运行任务继续使用其启动时固定的版本。

**DELETE /api/workflows/{id}**：被默认或任务引用时返回 409 `workflow_in_use`。

## 8. 生成

**POST /api/shots/{id}/generate**

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| target | string | 是 | 枚举 keyframe / clip / audio（TTS 补救）/ bgm / sfx |
| workflow_id | string | 否 | 缺省按匹配规则自动选择，传此字段则跳过匹配直接使用指定工作流 |
| params | object | 否 | 不得包含结构参数 |
| generation_mode | string | 否 | 枚举 text_only / image_to_video / reference_guided；缺省按 target 与项目设置确定，并在响应/任务快照中返回最终模式 |
| use_reference | bool | 否 | 兼容字段；新接口优先使用 generation_mode。reference_guided 下缺少必需参考图时返回 422，不得静默降级；显式确认降级后才可改为 text_only |

返回 202 与 job 对象，并返回/持久化 `effective_inputs`（实际注入的资产 ID 与版本、工作流版本、参数）。一致性模式下锚定资产存在但未锁定时返回 409；缺少必需参考图时返回 422 `workflow_media_missing` 或 `no_matching_workflow`。单镜、批量和重做共享同一输入解析器。

**POST /api/characters/{id}/generate**：生成角色定妆图，`target` 固定为 `keyframe`，以角色的 `appearance` 作为 prompt 基础，无需参考图，不受锚定锁定校验限制。可选 `workflow_id`、`params`（不含结构参数）。返回 202 与 job 对象。成功后新资产以 `asset_type=character` 落库，并回写 `character.portrait_asset_id`。

**POST /api/props/{id}/generate**：生成道具锚定图，规则同角色定妆图。成功后新资产以 `asset_type=prop` 落库，并回写 `prop.anchor_asset_id`。

**POST /api/scenes/{id}/generate-baseline**：生成场景基准图，`target` 固定为 `keyframe`，以场景的 `title` 与 `description` 作为 prompt 基础。成功后新资产以 `asset_type=scene` 落库，并回写 `scene.baseline_asset_id`。

**POST /api/scenes/{id}/generate**：批量生成场景内镜头，可选 `shot_ids` 限定范围；任务按并发 1 串行入队。此接口不生成场景基准图。选中镜头数少于 3 个时返回 400，提示改用单镜生成接口。视频任务串行约束：按 `seq` 升序处理，前一镜头视频成功并抽出末帧后才开始下一镜头；任一镜头失败时仅暂停依赖该镜头产物/末帧的后续镜头，无依赖关系的镜头继续；已完成镜头按状态跳过；显式指定 `shot_ids` 时仍按 `seq` 升序串行，不按传入顺序。

**POST /api/projects/{id}/generate**：跨场景批量生成。请求体含 `shot_ids`（可选）、`episode_from`/`episode_to`（可选，集数闭区间）、`scene_ids`（可选）、`target`（必填，keyframe 或 clip）、`workflow_id`（可选）。执行规则：

- 选中镜头数少于 3 个时返回 400，提示改用单镜生成接口
- 执行顺序固定为集数升序 → 场景在该集的出场顺序 → 镜头 `seq` 升序，全部串行
- 末帧衔接只在同场景同集内成立：跨场景或跨集时不注入 `ref_prev_frame`，改由 `start_state` 文字衔接；系统在生成确认弹窗中提示这一降级
- 任一镜头失败时仅暂停依赖该镜头视频或末帧的后续镜头；独立场景/集继续执行。用户可选择『重试失败项』『从失败点继续』或『忽略失败强制继续』；强制继续需提示可能损失连续性，并在 job 的 warnings 中记录 `prev_frame_skipped`，产物仍标记 succeeded 但 consistency verdict 降级为 review
- 已存在的 keyframe/clip 产物按状态跳过，不重复生成

**POST /api/shots/{id}/regenerate**：重做单镜，旧资产转为历史版本，不物理删除。

### 同一镜头多版本 job 的有效性判定

同一镜头、同一 `target` 因重做或重试产生多个 job 时：

- 仅当产物 `input_snapshot` 与当前镜头提示词、参考资产版本、工作流版本、模型参数及前镜尾帧版本匹配时，才可成为当前有效产物
- 依赖快照相同的候选中，以 `finished_at` 最新且状态为 `succeeded` 的 job 产物为当前有效产物
- 其余历史 job 产物保留为资产历史版本，不物理删除，可在资产详情中回滚
- 任务的 `result.asset_id` 指向其自身产物；镜头详情的 `current_assets` 返回各 target 的当前有效资产
- 判定不看 job 创建顺序，只看成功时间，避免重试产生的旧 job 覆盖新产物

## 9. 任务

**GET /api/jobs**：可选 `status`、`job_type`、`project_id`、`updated_since`、`updated_since_seq`、`page`、`page_size`。增量补齐以 `updated_since_seq` 为准，返回 `seq` 大于该值的任务，按 `seq` 升序；`updated_since` 时间戳仅作兼容保留，同一毫秒内多条更新时可能漏取或重复，不得单独依赖。

**GET /api/jobs/stats**：返回任务统计，用于任务中心顶部统计卡片。响应字段：`today_succeeded`（今日成功数）、`today_failed`（今日失败数）、`in_progress`（进行中数量，含 queued 与 running）、`avg_duration_seconds`（平均耗时，近 7 日已完成任务均值，无数据为 null）。支持可选 `project_id` 过滤；今日按服务本地时区 0 点起算。

**GET /api/jobs/{id}**：返回任务对象，含 `error_code`、`error_msg`、`retryable`、`result`。`retryable` 决定界面显示重试按钮还是指引链接。

**POST /api/jobs/{id}/retry**：仅 `failed` 或 `lost` 且 `retryable=true` 可重试，生成新的任务 ID。

**POST /api/jobs/{id}/cancel**：仅 `queued` 或 `running` 可取消；`running` 时调用 ComfyUI 中断接口（全局中断，见 ARCHITECTURE.md 5.2）；成功返回 204。

重试上限约定：同一任务的自动与手动重试累计上限由设置项 `job_max_attempts` 控制（默认 3，范围 1–10）；达到上限后 `retryable` 置为 false，界面改为展示指引，防止无限重试造成算力与费用浪费。

## 10. 合成与导出

**POST /api/projects/{id}/compose**

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| episode | int | 否 | 指定集数则只合成该集的镜头；缺省合成全部集，按 episode 升序串联 |
| include_subtitle | bool | 否 | 默认 true |
| subtitle_mode | string | 否 | 枚举 burn / sidecar，默认 burn |
| bgm.asset_id | string | 否 | 兼容单 BGM；复杂音轨使用 `timeline_tracks` |
| timeline_tracks | array | 否 | 时间线轨道数组；每轨含 role（dialogue/sfx/bgm）、asset_id、asset_version、shot_id/scene_id/episode、start/end、volume、fade_in/fade_out、loop_policy |
| bgm.volume | number | 否 | 0–1，默认 0.3 |
| compose_tolerance | number | 否 | 音画不等长容差（秒），0–2，默认 0.5 |

返回 202 与 job 对象。合成任务完成后 `result` 含 `composition_id`、`file_path`（内部预览，不直接作为导出下载地址）、`duration`、`srt_path`（sidecar 模式时存在）与 `warnings` 数组。

时间轴计算、音画对齐、字幕切分与混音规则见 PRD.md M7 节。合成前校验不通过时返回 422，`error.code` 可为 `output_missing`、`duration_mismatch_review_required`、`speaker_mapping_required`；项目非 active 状态返回 409。存在未审核放行的超容差镜头时整个合成请求被拒绝。

**GET /api/projects/{id}/compositions**：返回合成版本列表，含 `composition_id`、`episode`、`duration`、`stale`、`created_at`、`file_path`（预览用相对路径）。按 `created_at` 降序排列，支持 `episode` 筛选参数。

**GET /api/projects/{id}/compositions/{comp_id}**：返回合成版本详情，含 `source_snapshot`、`warnings`、关联 `job_id` 及产物预览地址。

**POST /api/projects/{id}/export**：独立创建导出任务。请求字段：`composition_id`（必填，必须为当前有效且未过期的合成版本）、`format`（可选，枚举 `mp4`，默认 `mp4`；剪映草稿等工程文件导出为后续规划）、`include_subtitle`（可选，默认沿用合成配置）、`subtitle_mode`（可选，默认沿用合成配置）、`delivery_profile`（可选，枚举 `standard`/`high_quality`，默认 `standard`；standard 使用合成产物直接封装，high_quality 在导出阶段重新编码为更高码率）。返回 202 与 job 对象。导出任务成功后创建 export 记录，保存来源 `composition_id`、文件名、大小、校验状态和创建时间。导出只负责交付封装，不改变 composition 状态。来源合成过期时返回 409 `composition_stale`；不存在成功合成时返回 409 `composition_required`。

导出任务不得隐式触发重新合成；需要更新成片时必须先调用 compose，再用新 `composition_id` 调用 export。重新导出可重复创建任务，相同参数可通过幂等键避免重复提交。

**GET /api/projects/{id}/exports**：返回导出记录列表，含 `id`、`composition_id`、`file_name`、`size`、`created_at`、`status`、`download_url`。其中 `download_url` 为下载接口的完整路径（`/api/exports/{id}/download`），客户端可直接拼接服务端 base URL 使用。不返回服务器绝对路径。

**GET /api/exports/{id}/download**：以流式响应返回导出文件，设置 `Content-Disposition: attachment`。文件不存在返回 404 `invalid_request`。

## 11. 设置与健康

**GET /api/settings**：返回全部配置；密钥字段返回掩码，并附带 `api_key_set` 布尔值表示是否已配置。

**PUT /api/settings**：部分更新；密钥字段传空字符串表示清除。

**POST /api/settings/test-comfyui**：请求体 `base_url`、`api_key`（可选）。失败时 `detail.reason` 取值为 `connection_refused` / `timeout` / `unauthorized` / `invalid_response` / `dns_error`。

**POST /api/settings/test-llm**：请求体 `base_url`、`api_key`（可选）、`model`。发送一次最小对话请求，返回耗时与回显内容。失败时 `detail.reason` 取值同 test-comfyui。

**POST /api/settings/backup**：手动触发一次备份，返回 202 与 job 对象。备份完成后 `result` 含备份文件路径、大小与校验结果。并发触发时若已有备份任务在跑，返回 409 `invalid_state_transition`。

**POST /api/settings/clear-upload-cache**：清理上传缓存（设置-存储页使用）。仅删除已入库资产对应的上传临时文件与未被任何资产引用的 upload_record 残留，不影响已生成资产与正在运行任务引用的文件。响应：`freed_bytes`（释放字节数）、`removed_files`（清理文件数）、`skipped_in_use`（因被引用而跳过的数量）。清理前由前端二次确认。频控：60 秒内重复调用不重新扫描文件系统，直接返回上次清理结果并附 `cached: true` 标记。

**GET /api/settings/channels**：返回三方生成通道配置列表（设置-三方通道页使用）。每条通道含 `channel_id`、`provider`（供应商标识）、`name`（显示名）、`base_url`、`api_key`（掩码）、`api_key_set`（是否已配置）、`enabled`（是否启用）、`priority`（降级尝试顺序，数字越小越先尝试）、`supports`（能力声明，如支持的 target 类型）。密钥仅返回掩码，不返回明文。

**PUT /api/settings/channels**：部分更新通道配置。请求体含 `channel_id` 与待更新字段：`name`、`base_url`、`api_key`（传空字符串表示清除，掩码值不视为变更）、`enabled`、`priority`。`priority` 在启用通道内必须唯一，冲突返回 400 `invalid_request`。更新即时生效，不影响已在执行的任务（任务固定使用提交时的通道）。响应返回更新后的通道对象（密钥掩码）。

**POST /api/settings/channels/{channel_id}/test**：测试三方通道连通性。发送一次最小探测请求，不提交生成任务、不产生云端计费。成功返回耗时与回显内容；失败时 `detail.reason` 取值同 test-comfyui（`connection_refused` / `timeout` / `unauthorized` / `invalid_response` / `dns_error`）。

三方通道降级规则（权威定义见 ARCHITECTURE.md 5.8 节）：仅当本地 ComfyUI 不可用（`comfy_unreachable` / `comfy_timeout`）且存在启用通道时，按 `priority` 升序逐个尝试；全部通道失败时任务按原失败分类处理，不静默丢弃。通道切换与降级过程写入 job 的 `input_snapshot` 与 warnings，可追溯。

**GET /api/health**：返回服务健康状态，含 `status`（healthy/degraded/down）、`database`（ok/error）、`comfyui`（ok/unreachable）、`worker`（ok/idle）、`disk`（剩余空间百分比）。用于设置页关于区域展示。

## 12. 事件流

**GET /api/events**：SSE 实时事件流。

| 项 | 约定 |
| --- | --- |
| 响应类型 | `text/event-stream` |
| 必备响应头 | `Cache-Control: no-cache`、`X-Accel-Buffering: no` |
| 事件类型 | `job.created`、`job.updated`、`job.finished`、`project.updated`、`asset.created`、`asset.updated`（资产状态变更如 stale/superseded/locked/unlocked，用于前端实时感知失效传播）、`shot.updated`（镜头状态变更如 pending→generating→review→done→redo，用于分镜看板实时刷新卡片角标） |
| 事件数据 | JSON，含 `event_type`、`id`、`status`、`updated_at`。`event_type` 取值与上表一致，与 `job_type`、`asset_type`、`workflow_type` 三套业务枚举互不通用，不得交叉赋值。`job.finished` 事件额外含 `result`：解析任务含 scenes 和 characters 数组，生成任务含 asset_id 和 file_path |
| 断线重连 | 3 秒间隔重连，重连后调用 `GET /api/jobs?updated_since_seq=<最后事件 seq>` 补齐状态，以递增序号为唯一依据；`updated_since` 时间戳仅作兼容保留 |
| 事件序号 | 每个事件带全局递增 `seq` 字段，跨项目连续不重置；断线补齐按 `seq` 严格大于判定，同毫秒多事件不漏取不重复 |
| 鉴权演进 | 本期不鉴权。后续引入 Bearer 时，EventSource 无法附加自定义请求头，前端需改用 fetch 配合 ReadableStream 读取流，服务端接口形态不需变化 |
