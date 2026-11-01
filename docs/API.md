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
| asset_locked | 409 | 资产已锁定，禁止删除或覆盖；分镜图锁定后禁止重新生成或替换，需先解锁 |
| asset_in_use | 409 | 资产被镜头引用，需先解绑 |
| workflow_in_use | 409 | 工作流被默认或任务引用，禁止删除 |
| invalid_state_transition | 409 | 状态跃迁非法 |
| script_version_conflict | 409 | 剧本版本与用户确认版本不一致 |
| parse_version_conflict | 409 | 候选解析版本已过期或当前版本已变化 |
| job_submission_unknown | 409 | 远端提交结果未知，需要对账后处理 |
| prev_frame_missing | 409 | 镜头声明需要上一镜尾帧（requires_prev_frame=True）但前一镜尚无可用尾帧，需先生成前序镜头视频并完成尾帧提取 |
| assets_review_pending | 409 | 公共资产尚未通过人工审核，禁止生成分镜图；需先在资产库提交审核并通过 |
| assets_review_rejected | 409 | 公共资产审核被驳回，需修改或替换资产后重新提交审核 |
| keyframe_not_confirmed | 409 | 分镜图尚未人工确认，禁止生成分镜视频；需先在分镜图环节逐镜确认 |
| keyframe_not_ready | 409 | 分镜图尚未生成，无法生成分镜视频 |
| job_in_flight | 409 | 同镜头同目标已有排队/运行中任务，拒绝重复提交 |
| no_pending_plans | 409 | 没有符合条件且待生成的资产定义 |
| no_generatable_plans | 409 | 按定义批量生成时无可生成项（全部被跳过） |
| plan_not_found | 404 | 资产定义不存在 |
| plan_not_generated | 409 | 定义尚未生成产物，无法确认 |
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
| PATCH | /api/projects/{id} | 更新项目（settings 含风格字段时自动规范化） |
| GET | /api/projects/style-options | 风格配置可选项下发（类型分组/叙事风格/视觉风格/节奏/情绪/枚举/默认值） |
| GET | /api/projects/{id}/style-config | 读取项目风格配置（规范化后 + 校验问题） |
| PUT | /api/projects/{id}/style-config | 保存项目风格配置（返回规范化结果与问题列表） |
| DELETE | /api/projects/{id} | 删除项目 |
| POST | /api/projects/{id}/archive | 归档项目 |
| POST | /api/projects/{id}/unarchive | 取消归档 |
| POST | /api/projects/{id}/assets-review | 公共资产人工审核（body: action=approve/reject；approve 前校验角色定妆图/场景基准图/道具锚定图齐全，缺项返回 409 assets_incomplete；通过后放行分镜图生成） |

### 剧本与分镜
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /api/projects/{id}/script-versions | 保存剧本（单版本覆盖，版本号+1） |
| GET | /api/projects/{id}/script-versions/current | 获取当前剧本文本（回显） |
| POST | /api/projects/{id}/script-agent | 剧本一体化分析（Agent：评估+解析合并，集数由 Agent 决定） |
| GET | /api/projects/{id}/script-agent/status | Agent 分析状态（job + 口径 + 集级记录） |
| POST | /api/projects/{id}/estimate | 评估可制作集数（兼容保留，新流程由 script-agent 替代） |
| POST | /api/projects/{id}/estimates/{estimate_id}/confirm | 确认评估与制作范围（兼容保留） |
| POST | /api/projects/{id}/parse | 解析剧本（按选定集范围；兼容保留） |
| POST | /api/projects/{id}/parse-versions/{parse_version_id}/confirm | 确认候选解析版本 |
| GET | /api/projects/{id}/parse/status | 解析状态（job + 集级解析记录） |
| POST | /api/projects/{id}/parse/retry | 重试失败解析（从失败集继续） |
| POST | /api/projects/{id}/script-write | AI 创作/扩写剧本（阶段1，mode=create/expand） |
| POST | /api/projects/{id}/script-evaluate | 剧本质量评估（阶段2，返回结构化评估报告） |
| GET | /api/projects/{id}/scenes | 场景列表，可选 include_shots |
| POST | /api/scenes | 创建场景 |
| GET | /api/scenes/{id} | 场景详情（含出场集数与基准图） |
| PATCH | /api/scenes/{id} | 更新场景 |
| DELETE | /api/scenes/{id} | 删除场景（级联其下镜头） |
| POST | /api/scenes/{id}/episodes | 追加场景出现集数 |
| DELETE | /api/scenes/{id}/episodes/{episode} | 移除场景某集出场 |
| GET | /api/scenes/{id}/shots | 场景内镜头列表（按集，分页） |
| GET | /api/shots/{id} | 镜头详情（含一致性、聚合素材） |
| GET | /api/projects/{id}/shots | 按集拉取全部分镜（跨场景扁平列表） |
| POST | /api/shots | 创建镜头 |
| PATCH | /api/shots/{id} | 更新镜头 |
| DELETE | /api/shots/{id} | 删除镜头 |
| PUT | /api/scenes/{id}/shots/order | 镜头排序 |
| POST | /api/shots/{id}/keyframe/confirm | 确认单镜分镜图（D-13 静图闸口放行） |
| POST | /api/shots/{id}/keyframe/unconfirm | 取消单镜分镜图确认 |
| POST | /api/projects/{id}/keyframe/confirm | 批量确认项目内分镜图（body: shot_ids，省略则全部） |
| DELETE | /api/shots/{id}/assets/{asset_id} | 解绑镜头与资产的引用关系 |
| GET | /api/shots/{id}/regenerate/config | 重做配置初值（槽位、参考图、提示词） |
| POST | /api/shots/{id}/regenerate/workbench | 重做工作台提交（结果落候选版本，不覆盖生效版本） |
| GET | /api/shots/{id}/impact | 受影响下游镜头清单（只标记，不自动重生成） |
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
| GET | /api/assets/{id}/file | 媒体文件服务（原文件/缩略图） |
| GET | /api/assets/{id}/versions | 资产版本时间线 |
| POST | /api/assets/{id}/lock | 锁定资产 |
| POST | /api/assets/{id}/unlock | 解锁资产 |
| POST | /api/assets/{id}/rollback | 回滚版本 |
| DELETE | /api/assets/{id} | 删除资产 |
| POST | /api/assets/batch/delete | 批量删除资产 |
| POST | /api/assets/batch/lock | 批量锁定资产 |
| POST | /api/assets/batch/unlock | 批量解锁资产 |
| POST | /api/assets/export | 打包导出资产 |
| GET | /api/assets/{id}/regenerate/config | 重新生成弹窗初值（提示词由阶段 B 编译器产出一致） |
| POST | /api/assets/{id}/regenerate | 重新生成资产（可带自定义提示词，202） |
| POST | /api/assets/{id}/replace | 用上传文件替换资产生成新版本 |
| GET | /api/assets/{id}/candidates | 同镜头同类型的候选版本列表 |
| POST | /api/assets/{id}/adopt | 采用候选版本（原版本转历史，实体指针同步） |
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
| POST | /api/jobs/projects/{id}/retry-failed | 批量重试项目内失败任务（body: job_ids） |
| GET | /api/jobs/{id}/llm-stream | LLM 调用流式输出（进行中任务的增量内容） |
| GET | /api/jobs/{id}/snapshot | 任务输入快照（含 prompt_compilation 编译产物） |

### 合成与导出
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /api/projects/{id}/compose | 合成可预览成片 |
| GET | /api/projects/{id}/compositions | 合成版本列表 |
| GET | /api/projects/{id}/compositions/{comp_id} | 合成版本详情 |
| POST | /api/projects/{id}/export | 创建独立导出任务 |
| GET | /api/projects/{id}/exports | 导出记录列表 |
| GET | /api/exports/{id}/download | 下载导出文件 |
| GET | /api/projects/{id}/exports/{export_id}/acceptance-check | 成片验收检查（分辨率/时长/音轨/字幕） |
| GET | /api/media/{file_path} | 媒体文件服务（限定 media_dir 内，防路径穿越） |

### 资产定义（AssetPlan）
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /api/projects/{id}/asset-plans | 查询项目资产定义（可按 plan_type/status/episode 过滤，可选 with_dependencies） |
| POST | /api/projects/{id}/asset-plans/generate | 生成资产定义计划 |
| POST | /api/projects/{id}/asset-plans/{plan_id}/confirm | 确认单条资产定义 |
| POST | /api/projects/{id}/asset-plans/{plan_id}/unconfirm | 取消确认 |
| POST | /api/projects/{id}/asset-plans/batch-confirm | 批量确认资产定义 |

### 设置与事件流
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /api/settings | 获取设置 |
| PUT | /api/settings | 更新设置 |
| POST | /api/settings/test-comfyui | 测试 ComfyUI 连通性 |
| POST | /api/settings/test-llm | 测试语言模型连通性 |
| POST | /api/settings/backup | 手动触发备份 |
| POST | /api/settings/clear-upload-cache | 清理上传缓存 |
| GET | /api/settings/comfyui-instances | 获取 ComfyUI 实例列表（多实例加权负载均衡） |
| PUT | /api/settings/comfyui-instances | 更新 ComfyUI 实例列表 |
| POST | /api/settings/comfyui-instances/test | 逐实例测试连通性 |
| GET | /api/settings/comfyui-instances/status | 实例健康状态汇总（含队列深度与最近错误） |
| POST | /api/settings/llm-models | 拉取可用模型列表（按 Provider 探测） |
| GET | /api/settings/prompts | 列出全部提示词（D-15） |
| GET | /api/settings/prompts/{name} | 读取提示词内容（D-15） |
| PUT | /api/settings/prompts/{name} | 写入提示词（D-15） |
| POST | /api/settings/prompts/{name}/reset | 重置提示词为内置默认值（D-15） |
| GET | /api/health | 服务健康状态 |
| GET | /api/events | 实时事件流（SSE） |
| GET | /api/events/backfill | 事件增量补齐（按 event_seq） |

> 导演台 Agent 会话的 12 条路由（`/api/agent/sessions*`）为独立专题，完整清单见 §12 路由总览，此处不重复列出。

---

## 4. 项目管理（字段级契约）

**POST /api/projects**

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| name | string | 是 | 1–100 字符 |
| aspect_ratio | string | 是 | 枚举 9:16 / 16:9 / 1:1 |
| creation_type | string | 否 | 枚举 novel / script，默认 novel |
| description | string | 否 | ≤500 字符 |
| style_config | object | 否 | 风格配置子集。提供时以默认值为基线合并后规范化落库；未提供或留空则完全使用默认风格。字段与取值见 STYLE_CONFIG.md |

响应 201，返回项目对象，初始 `status` 为 `draft`。

**GET /api/projects**：支持 `status`、`keyword`、`page`、`page_size`。

**GET /api/projects/{id}**：返回项目对象，含 `shot_total`、`shot_done`、`current_stage`（派生规则见 ARCHITECTURE.md 第 4 节）。

**PATCH /api/projects/{id}**：可改 `name`、`description`、`settings`。`settings` 可含 `default_image_workflow_id`、`default_video_workflow_id`、`default_tts_workflow_id`、`consistency_pass_threshold`、`consistency_fail_threshold`（须满足 fail 小于 pass，否则返回 400），以及产能参数 `episode_duration_target`（默认 90 秒，软指标）、`shots_per_episode_target`（默认 9）、`shot_duration_default`（默认 10 秒，仅作生成参考）、`max_episodes_per_batch`（单次制作建议上限，默认 10）。

`settings` 中若含风格字段（`genre_tags`、`narrative_styles`、`visual_style`、`pacing`、`emotional_tone`、`conflict_intensity`、`twist_density`、`dialogue_style`、`narration_mode`、`target_audience`、`custom_style_requirements`）或旧键 `genre`、`audience`、`requirements`，将自动执行规范化并写入 `style_config_version`；非风格键（如 `episode_duration_target`）原样保留。字段含义与取值见 STYLE_CONFIG.md。

**DELETE /api/projects/{id}**：存在进行中任务时返回 409；成功执行软删除并返回 204。删除后进入延迟清理期，恢复期内可恢复，任务引用的文件不立即物理删除。

**POST /api/projects/{id}/archive**：仅 `active` 可调用，置为 `archived`，返回项目对象。

**POST /api/projects/{id}/unarchive**：仅 `archived` 可调用，置回 `active`，返回项目对象。

**GET /api/projects/style-options**
返回 `version`、`genre_groups`（分组名 + 标签数组）、`genre_tags`、`narrative_styles`、`visual_styles`、`pacing`、`emotional_tones`、`conflict_intensity`、`twist_density`、`dialogue_styles`、`narration_modes`、`target_audiences`（枚举项均为 `{value, label, description}`）、`max_custom_length`、`defaults`（默认配置）。前端据此渲染配置面板，禁止硬编码词表。

**GET /api/projects/{id}/style-config**
返回 `config`（规范化后的完整配置）、`version`（当前 `STYLE_CONFIG_VERSION`）、`issues`（校验问题数组，每项含 `code`、`message`，可选 `severity`）、`visual_style_label`、`visual_style_prompt`、`raw`（仅含请求中实际出现的风格键原值）。

**PUT /api/projects/{id}/style-config**
请求体为风格字段的任意子集，未提供字段沿用当前值。返回 `config`、`version`、`issues`、`visual_style_label`、`saved`。校验问题不阻断保存，异常取值按 STYLE_CONFIG.md 第 5 节规则修正（未知视觉风格回退默认、超长自定义文本截断等）。保存后 `project.settings` 同时写入 `visual_style` 与 `style_config_version`。

## 5. 剧本与分镜

**POST /api/projects/{id}/script-versions**：保存剧本文本（单版本覆盖模式）：每项目仅保留一条当前版本记录，保存时覆盖文本、版本号+1，`script_version_id` 不变。请求体 `text` 为 1–50000 字符；成功返回 `script_version_id`、`version`、`content_hash`。后续评估与解析均需引用该版本。

**GET /api/projects/{id}/script-versions/current**：获取当前剧本（单版本覆盖模式）。返回 `{exists, text, script_version_id, version}`；未保存过时 `exists` 为 false。供前端页面加载回显。

**POST /api/projects/{id}/estimate**：先评估再解析的第一阶段，不产出分镜。

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| script_version_id | string | 是 | 本次评估使用的不可变剧本版本 |
| text | string | 是 | 1–50000 字符；内容哈希必须与 script_version_id 一致 |
| episode_duration_target | number | 否 | 单集目标时长（秒），缺省取项目 settings，默认 90 |

响应直接返回评估对象（非长耗时任务），含 `char_count`、`dialogue_char_estimate`、`estimated_total_duration`、`estimated_episodes`、`episode_breakdown`（每集含 episode、estimated_duration、estimated_shots）、`exceeds_batch_limit`、`suggestion`（超限时提示剧本过长、建议裁剪或分批）。

估算算法：对白语速模型只作为估算输入之一；应结合旁白、动作/纯视觉、停顿和转场的估算时长形成区间，并记录算法/提示词版本及主要假设。字符数/语速计算仅可作为缺少结构信息时的 fallback，不可作为唯一的集数判定依据。评估结果必须持久化到 `script_estimate`；`project.estimated_at` 与 `estimated_text_hash` 仅作进度推导/兼容缓存，不替代评估历史记录。

**POST /api/projects/{id}/estimates/{estimate_id}/confirm**：确认评估结果与制作范围。请求体含 `script_version_id`、用户确认的 `episode_start`、`episode_count` 及可选时长调整；评估版本与选择不匹配时返回 409 `script_version_conflict`。确认后 `script_estimate.user_confirmed=true`。

**POST /api/projects/{id}/script-agent**：剧本一体化分析（Agent 流程，推荐入口）。评估与解析合并为一次任务，集数由 Agent 决定，无需用户确认集数。
| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| script_version_id | string | 是 | 不可变剧本版本 |
| text | string | 是 | 1–50000 字符；内容哈希必须与 script_version_id 一致 |
| episode_start | int | 否 | 缺省 1；后续批次继续分析时传入首个未完成集数 |

内部流程：算法预检（estimate_script，不落库）→ 软闸口评估（evaluate_script，失败不阻断，仅记录 agent.eval_info 事件）→ 自动分批 LLM 解析（复用 parse_system 提示词，每批 ≤ max_episodes_per_batch）→ 口径锁定（首批成功后 total_episodes + script_hash 写入 ParseVersion）→ 集级记录（EpisodeParseRecord，状态机同 parse）。返回 `job_id`（202）。

首批自动 + 后续半自动：`agent_first_batch_auto=true`（默认）时首次调用仅解析首批（≤ max_episodes_per_batch 集），`job.result.has_more=true` 提示前端展示『继续分析』入口；后续批次由前端携带 episode_start 再次触发。剧本 hash 变化则口径失效，需重新全量分析。

**GET /api/projects/{id}/script-agent/status**：返回最近一个 script_agent 任务（job 含 status/error/result）、口径（scope：total_episodes/script_hash/parse_version_id/parse_version_status）与集级记录（records：episode_no/status/asset_count/error_msg）。前端进度时间线与集数网格数据源。

**POST /api/projects/{id}/parse**

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| script_version_id | string | 是 | 必须指向用户确认的不可变剧本版本 |
| text | string | 是 | 1–50000 字符；必须与 script_version_id 的内容哈希一致 |
| episode_start | int | 否 | 本次解析的起始集数，缺省为 1 |
| episode_count | int | 否 | 本次解析的集数，缺省由解析器自主分析确定 |
| replace_existing | bool | 否 | 默认 false。true 时仅在用户确认候选解析差异后替换选定且未锁定的集；不得直接覆盖当前版本 |
| llm_model | string | 否 | 缺省使用设置中的模型 |

解析不再强制前置确认评估，集数由解析器自主分析确定，未评估也可直接提交解析并返回 202；版本哈希不匹配时返回 409 `script_version_conflict`。解析仅生成 `parse_version.status=candidate` 的候选版本，范围外的集不落库。返回 `job_id`；解析任务通过 SSE `job.finished` 事件通知结果，前端也可轮询 `GET /api/jobs/{job_id}`。用户确认后由确认接口在事务中切换 `project.current_parse_version_id`，并标记旧版本为 superseded。候选解析失败或未确认时当前分镜保持不变。

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

**POST /api/scenes**：`project_id`、`title`、`description`、`episode`（可选，默认 1，正整数）、`seq`（可选，缺省追加项目末尾）。创建时自动写入一条 scene_episode 记录。场景标题与角色名不得相同（@引用无法区分），同名返回 409（D-16 修订）。

**POST /api/scenes/{id}/episodes**：追加出现集数，请求体 `episode` 与 `seq_in_episode`。重复添加同一集返回 409。响应对象含 `requires_parse` 布尔值：追加的集数超出当前已解析范围时为 true，提示该集尚无分镜产物、须先追加解析方可进入生成（规则见 PRD.md M2 业务规则）；在已解析范围内时为 false。

**DELETE /api/scenes/{id}/episodes/{episode}**：移除该场景在某集的出场。移除最后一集时拒绝，须改用删除场景接口。该集下已存在镜头时返回 409 `asset_in_use`，需先删除该集镜头；不提供级联删除。

**PATCH /api/scenes/{id}** 与 **DELETE /api/scenes/{id}**：删除级联其下镜头，需前端二次确认。

**POST /api/shots**：`scene_id`、`episode`（必填）、`shot_kind`（必填，枚举 dialogue/narration/visual/transition）、`dialogue`（可选；shot_kind 为 dialogue/narration 时必填）、`image_prompt`、`video_prompt`（可选）、`duration`、`requires_prev_frame`（可选，默认 false，P0 尾帧依赖声明：为 true 时生成 clip 必须有上一镜可用尾帧，否则返回 409 `prev_frame_missing`）。约束：`(scene_id, episode)` 必须在 scene_episode 中存在，否则返回 422；该约束由创建接口校验，不依赖数据库外键。

**GET /api/projects/{id}/shots**：按集拉取跨场景扁平镜头列表。`episode` 必填，可选 `page`、`page_size`。排序：先按场景在该集出场顺序（scene_episode.seq_in_episode）升序，同场景内按镜头 seq 升序。每条镜头附带 `scene_title`、`scene_seq` 与 `current_assets`。

**GET /api/shots/{id}**：返回镜头对象，含 `consistency` 字段（`score` 0–1，`verdict` 枚举 passed/review/failed），并返回聚合素材：`scene`（id/seq/title/episode）、`character`（id/name/appearance/portrait_asset_id，无则 null）、`props`（id/name/anchor_asset_id 数组）、`current_assets`（keyframe/clip/audio 三键，各含 asset_id/file_path/thumb_path/url/thumb_url/version，未生成为 null）。

`current_assets` 取值口径：取依赖版本与当前输入匹配、状态为 current 且来源任务 succeeded 的产物；完成时间仅用于同一有效输入快照下的排序，不能覆盖更新输入对应的有效产物（详见第 8 节多版本任务有效性规则）。

**PATCH /api/shots/{id}**：可修改 `image_prompt`、`video_prompt`、`dialogue`、`duration`、`requires_prev_frame` 或镜头结构字段。修改 `requires_prev_frame` 不触发 stale 标记（仅影响后续生成时的尾帧预检行为），其余字段变更后按依赖矩阵标记相关产物 `stale`，不自动触发任务。仅修改 `video_prompt` 时只影响视频，分镜图不受影响。修改 `duration` 时若新值超出 H3 支持范围（4–15 整数秒），返回 warning 字段提示 `duration_out_of_range`，同时仍接受修改并标记相关产物 `stale`。

**GET /api/scenes/{id}/shots**：返回该场景镜头数组，按 seq 升序。`episode` 必填；可选 `page`、`page_size`。同一场景在不同集的镜头相互独立。

**PUT /api/scenes/{id}/shots/order**：请求体 `episode` 与 `shot_ids` 数组。排序仅在同一场景同一集内进行，`shot_ids` 含其他场景或其他集的镜头 ID 时返回 400。

**GET /api/projects/{id}/characters**：返回项目内角色数组。

**POST /api/characters**：`project_id`、`name`、`description`、`traits`、`appearance`、`voice_id`（可选）。`name` 项目内唯一，重复返回 409；角色名与场景标题不得相同（@引用无法区分），同名返回 409（D-16 修订）。

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

**GET /api/assets/{id}**：返回资产对象，含 `versions` 数组与 `referenced_by` 镜头列表。资产对象含 `url`（原文件服务地址 `/api/assets/{id}/file`）与 `thumb_url`（缩略图服务地址 `/api/assets/{id}/file?thumb=true`，无缩略图为 null）字段，前端列表/看板优先加载 thumb_url。
**GET /api/assets/{id}/file**：媒体文件服务（D-17 配套）。返回资产原文件；`?thumb=true` 且缩略图存在时返回缩略图，缩略图缺失回退原文件；文件不存在返回 404 `asset_file_missing`。

**POST /api/assets/{id}/lock**：锁定后不可删除。无请求体，返回资产对象。

**POST /api/assets/{id}/unlock**：恢复可删除状态。无请求体，返回资产对象。

**POST /api/assets/{id}/rollback**：请求体 `target_version`。回滚生成新版本号，`parent_id` 指向被回滚版本；引用该资产的镜头标记『待重做』，但不自动触发任务。

**DELETE /api/assets/{id}**：被引用返回 409 `asset_in_use`，已锁定返回 409 `asset_locked`。

**POST /api/shots/{id}/assets**：请求体 `asset_id` 与 `role`。手动绑定仅允许 4 种 role：`reference` / `first_frame` / `end_frame` / `audio`；其余 role（identity/costume/scene/prop/prev_frame/dialogue/sfx/bgm）由系统在生成接口内部自动注入，不开放手动绑定。单镜头关联资产总数不得超过 6 个，超限返回 400；同一镜头重复绑定同一资产返回 409 `asset_in_use`。该接口用于手动覆盖自动注入结果；正常生成视频时由后端在 generate 内部自动完成绑定，无需前端调用。

**POST /api/assets/batch/delete**：请求体 `asset_ids` 数组。任一资产被锁定或引用时整批返回 409，不部分执行。

**POST /api/assets/batch/lock** / **POST /api/assets/batch/unlock**：请求体 `asset_ids` 数组。

**POST /api/assets/export**：请求体 `asset_ids` 数组，返回打包下载流。使用 POST 而非 GET，避免资产数量较多时 URL 过长触发网关限制。

## 6.5 资产定义（AssetPlan）

解析落库后由 `build_asset_plans` 一次性定稿五类定义（character/scene/prop/keyframe/clip）：名称 `{前缀}_{语义}_{短哈希}` 与占位路径一经生成永不修改，提示词随重新解析刷新。分镜图/视频提示词用 `@名称` 语法引用角色/场景（@引用解析可携带参考图）；`ref_image`/`ref_prev_frame`/`first_frame`/`end_frame` 为系统保留槽位，由执行器按镜头实际产物回填，提示词中不写 @ 引用。

**GET /api/projects/{id}/asset-plans**：可选 `plan_type`、`status`、`episode`、`with_dependencies`。返回 `items`（含 `asset_key`/`media_path`/`prompt`/`status`/`owner_id`/`confirmed`/`dependencies`）+ `counts`（按类型统计 total/pending/generated/rejected）+ `total`。

**POST /api/projects/{id}/asset-plans/generate**：请求体 `plan_type` 必填，`plan_ids`/`workflow_id` 可选（缺省取该类型全部 pending 定义）。闸口：keyframe 须公共资产审核通过（D-23）且跳过已锁定分镜图；clip 须分镜图已确认（D-13）。提交时按定义依赖注入参考图槽位（仅注入工作流 `capabilities.reference_slots` 声明的槽位），绑定快照（槽位/资产ID/版本/路径/状态）随任务参数持久化，重试复用快照不重新解析。返回 `jobs` + `count` + `skipped`（含跳过原因）。

**POST /api/projects/{id}/asset-plans/{plan_id}/confirm**：人工确认定义；keyframe 确认同步置 `shot.keyframe_confirmed=True`。未生成返回 409 `plan_not_generated`。

**POST /api/projects/{id}/asset-plans/{plan_id}/unconfirm**：取消确认；keyframe 同步回退 `shot.keyframe_confirmed=False`。

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

### 提示词编译前置校验（阶段 B，D-24）
所有生成通道在入队前先经过阶段 B 编译器（`app/services/prompt_compiler.py`），由 `generate.py` 的 `_compile_asset_prompt` / `_compile_shot_prompt` 统一调用。编译器输出写入 `job.input_snapshot.prompt_compilation`，包含 `task_type`、`template_id`、`template_version`、`positive_prompt`、`negative_prompt`、`reference_slots`、`generation_constraints`、`blocking_issues`、`warnings`。
- 编译状态 `ready` 才写入工作流参数并创建任务；`needs_review` 时不创建任务，按首个阻断项返回对应错误码。
- 编译期阻断错误码：`asset_identity_missing`、`assets_review_pending`、`character_portrait_missing`、`scene_baseline_missing`、`prop_asset_missing`、`storyboard_asset_missing`、`storyboard_file_missing`、`reference_asset_missing`、`reference_asset_file_missing`、`reference_capacity_exceeded`、`storyboard_not_approved`、`prev_frame_missing`、`prompt_template_missing`、`prompt_compilation_not_ready`（均返回 409 或 422，不静默降级、不空跑任务）。
- 只改提示词的补丁经 `assert_prompt_only_patch` 校验，工作流结构参数与槽位映射被锁定，越界返回 `prompt_only_patch_locked_fields`。
- 参考槽位只能取自工作流 `capabilities.reference_slots`；需求超过能力上限返回 `reference_capacity_exceeded`，未注入槽位的 `image{n}` 连线在提交前剔除，`<Picture N>` 按连线顺序重编号。

**POST /api/characters/{id}/generate**：生成角色定妆图，`target` 固定为 `keyframe`，以角色的 `appearance` 作为 prompt 基础，无需参考图，不受锚定锁定校验限制。可选 `workflow_id`、`params`（不含结构参数）。返回 202 与 job 对象。成功后新资产以 `asset_type=character` 落库，并回写 `character.portrait_asset_id`。

**POST /api/props/{id}/generate**：生成道具锚定图，规则同角色定妆图。成功后新资产以 `asset_type=prop` 落库，并回写 `prop.anchor_asset_id`。

**POST /api/scenes/{id}/generate-baseline**：生成场景基准图，`target` 固定为 `keyframe`，以场景的 `title` 与 `description` 作为 prompt 基础。成功后新资产以 `asset_type=scene` 落库，并回写 `scene.baseline_asset_id`。

**POST /api/scenes/{id}/generate**：批量生成场景内镜头，可选 `shot_ids` 限定范围；任务入队后由 worker 按并发 = 可用 ComfyUI 实例数执行（单实例串行，D-18）。此接口不生成场景基准图。选中镜头数少于 3 个时返回 400，提示改用单镜生成接口。视频任务串行约束：按 `seq` 升序处理，前一镜头视频成功并抽出末帧后才开始下一镜头；任一镜头失败时仅暂停依赖该镜头产物/末帧的后续镜头，无依赖关系的镜头继续；已完成镜头按状态跳过；显式指定 `shot_ids` 时仍按 `seq` 升序串行，不按传入顺序。@引用注入（D-16）：keyframe/clip 生成时提示词中的 @角色/@场景 自动携带参考图，与单镜生成行为一致。keyframe 目标受人工审核闸口约束（D-23）：公共资产未审核通过返回 409 `assets_review_pending`；已锁定的分镜图不允许被批量覆盖，命中时返回 409 `asset_locked`，需先在分镜图页解锁。

**POST /api/projects/{id}/generate**：跨场景批量生成。请求体含 `shot_ids`（可选）、`episode_from`/`episode_to`（可选，集数闭区间）、`scene_ids`（可选）、`target`（必填，keyframe 或 clip）、`workflow_id`（可选）。执行规则：

- 选中镜头数少于 3 个时返回 400，提示改用单镜生成接口
- 执行顺序固定为集数升序 → 场景在该集的出场顺序 → 镜头 `seq` 升序；入队后由 worker 按并发 = 可用 ComfyUI 实例数执行（单实例串行，D-18）
- @引用注入（D-16）：keyframe/clip 生成时逐镜头解析提示词中的 @角色/@场景 自动携带参考图，与单镜生成行为一致；多角色按出现顺序填入角色槽位（ref_identity → ref_image → ref_costume），槽位不足返回 422
- 末帧衔接只在同场景同集内成立：跨场景或跨集时不注入 `ref_prev_frame`，改由 `start_state` 文字衔接；系统在生成确认弹窗中提示这一降级
- 任一镜头失败时仅暂停依赖该镜头视频或末帧的后续镜头；独立场景/集继续执行。用户可选择『重试失败项』『从失败点继续』或『忽略失败强制继续』；强制继续需提示可能损失连续性，并在 job 的 warnings 中记录 `prev_frame_skipped`，产物仍标记 succeeded 但 consistency verdict 降级为 review
- 已存在的 keyframe/clip 产物按状态跳过，不重复生成

**POST /api/shots/{id}/regenerate**：重做单镜，旧资产转为历史版本，不物理删除。@引用注入（D-16）：keyframe/clip 重做时同样解析提示词中的 @角色/@场景 携带参考图，与单镜生成行为一致。

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

**GET /api/settings/comfyui-instances**：返回 ComfyUI 实例列表（设置-ComfyUI 页使用）。每条实例含 `base_url`（实例地址）、`weight`（权重，正整数，越大分配任务越多）、`enabled`（是否启用）。未配置实例时回退为单实例 `comfyui_base_url`。

**PUT /api/settings/comfyui-instances**：整体更新实例列表。请求体为实例数组，每项含 `base_url`、`weight`、`enabled`。更新即时生效，不影响已在执行的任务（任务固定使用提交时获取的实例）。
**GET /api/settings/prompts**：列出全部提示词（D-15）。每条含 `name`（提示词名）、`externalized`（是否已外置到 workspace/prompts/）、`sha256_8`（内容哈希前 8 位）、`length`（字符数）。
**GET /api/settings/prompts/{name}**：读取提示词内容。外置文件优先，缺失回退内置默认值；提示词不存在返回 404。
**PUT /api/settings/prompts/{name}**：写入提示词（创建或覆盖外置文件），保存即生效（每次调用动态读取，无需重启）。写入前自动备份当前版本（最多保留 5 份 .bak）。空内容返回 400；提示词名仅允许字母/数字/下划线/连字符，非法名称返回 400；未注册的提示词名返回 404。
**POST /api/settings/prompts/{name}/reset**：重置提示词为内置默认值（删除外置文件）。提示词不存在返回 404。

**POST /api/settings/comfyui-instances/test**：逐实例测试连通性。对每个实例发送最小探测请求（GET /system_stats），不提交生成任务。返回 `{ok, instances: [{base_url, enabled, weight, ok, latency_ms?, reason?, message?}]}`；失败时 `reason` 取值同 test-comfyui（`connection_refused` / `timeout` / `invalid_response`）。

多实例负载均衡规则（权威定义见 ARCHITECTURE.md 5.8 节）：生成任务按权重加权轮询选择启用实例；实例不可用时自动切换到其余启用实例（故障转移）；全部实例失败时任务按 `comfy_unreachable` 失败分类处理，不静默丢弃。实际使用的实例写入 job 的 `input_snapshot.comfyui_instance`，可追溯。

**GET /api/health**：返回服务健康状态，含 `status`（healthy/degraded/down）、`database`（ok/error）、`comfyui`（ok/unreachable）、`worker`（ok/idle）、`disk`（剩余空间百分比）。用于设置页关于区域展示。

## 12. 导演台 Agent 会话

### 路由总览
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | /api/agent/sessions | 创建会话（201） |
| GET | /api/agent/sessions | 会话列表（分页） |
| GET | /api/agent/sessions/{session_id} | 会话详情 |
| PATCH | /api/agent/sessions/{session_id} | 更新会话（标题/关联项目/模型） |
| DELETE | /api/agent/sessions/{session_id} | 归档会话（软删除，status=archived） |
| POST | /api/agent/sessions/{session_id}/restore | 恢复归档会话 |
| POST | /api/agent/sessions/{session_id}/messages | 追加消息 |
| GET | /api/agent/sessions/{session_id}/messages | 消息列表（支持 after_seq 增量） |
| POST | /api/agent/sessions/{session_id}/chat | 聊天（意图识别 → 工具审计 → LLM 回复） |
| GET | /api/agent/sessions/{session_id}/approvals | 会话内待审批生成申请列表 |
| POST | /api/agent/sessions/{session_id}/approvals/{confirmation_id}/resolve | 裁决审批（批准/拒绝，重复裁决 409） |
| POST | /api/agent/sessions/{session_id}/approvals/{confirmation_id}/execute | 执行已批准的生成申请（幂等，重复执行复用同一任务） |

### 会话与消息
- 会话 `project_id` 可空：未关联项目时为通用问答，不执行项目工具。
- 会话 `model` 字段可覆盖默认模型（快照，切换模型不回写历史消息）。
- 消息 `role` 取值：`user` / `assistant` / `system` / `tool_event`（工具调用事件）/ `approval`（人工审批事件）。
- 消息 `seq` 会话内自增（唯一索引兜底并发），前端按 seq 排序与增量加载（`after_seq`）。
- 归档会话只读：追加消息/聊天返回 409 `session_archived`。

### 聊天流程（POST /chat）
1. 持久化 user 消息。
2. **意图识别**（B 档）：先走确定性**规则快路径**（`apply_fast_path_rules`，结果 `source=rules`），未命中再调用 LLM 结构化识别（`source=llm`）。规则优先级 unsafe > generate_request > query，命中即返回（confidence=1.0，省一次 LLM 往返）：unsafe 拦索要密钥/凭据、删除或清空全部数据、绕过规则或审批；generate_request 要求生成动作 + 产物词（生成/出图/渲染 + 关键帧/分镜/视频/角色图等），并带负向断言排除描述性表述（『最近生成的角色图长什么样』是在查看已有产物，判为 query 而不是生成请求）；query 覆盖进展/进度/状态/概览、失败原因、任务列表、资产统计，以及查看类表述（看看/看一下/给我看看/长什么样 + 图/视频/片段/素材/角色/场景/道具/关键帧/分镜/资产）。未命中规则的模糊消息交由 LLM 输出经 Pydantic Schema 校验，提取 intent/goal/entities/missing_information/risk_level/confidence；规则兜底高风险关键词；置信度 < 0.7 或信息缺失时返回澄清问题（route_action=clarify），不进入主对话。
3. **确定性路由**：unsafe → 拒绝（reject）；generate_request → 落地为审批申请（guide 分支内 `_try_create_generate_approval`，只建申请绝不执行生成）；识别失败降级 query 继续主对话。
   生成申请的目标定位（不猜用户意图对象）：镜头目标须消息明确给出镜头标识（`sht_` ID 或『第 N 镜』且项目内唯一命中）；实体目标（角色/场景/道具公共资产）须明确给出实体 ID（`chr_`/`scn_`/`prp_` 前缀），不按名称猜测；定位不到时返回澄清提示。生成目标由关键词判定：音频（配音/语音/台词/朗读/TTS/旁白）→ audio，视频（视频/片段/动态）→ clip，图片类 → keyframe，判定不出默认 keyframe（成本最低）。
4. **工具调用审计**：会话关联项目时按意图-工具映射执行只读工具（query/analysis → project_overview、asset_stats、list_jobs、list_asset_plans、get_style_config；其余意图不授予任何工具），每次调用落一条 `role=tool_event` 消息（payload：tool_name/args/ok/error_code/duration_ms），在 LLM 调用前提交（LLM 失败不丢审计）；tool_event 不进入对话上下文。

   `list_assets` 为**条件注入**：仅当用户消息命中资产类关键词（图/视频/片段/素材/缩略/画面/看看/长什么样）时，才以 `validity=current, limit=6` 追加查询。既避免每条回复都附带缩略图（UI 噪音与无谓带宽），也只展示仍然有效的资产。
5. **LLM 回复**：历史窗口最近 40 条 user/assistant 消息；瞬态错误指数退避重试最多 3 次；`llm_unconfigured` → 409，其余失败 → 502（用户消息已持久化，可重试）。
6. assistant 消息 payload 记录：`intent` / `intent_reason` / `intent_classified` / `route_action` / `intent_goal` / `risk_level` / `confidence`。

### 工具注册表（TOOL_REGISTRY）
| 工具 | 权限 | 风险 | 副作用 | 审批 |
| --- | --- | --- | --- | --- |
| project_overview | read | low | 无 | 否 |
| list_jobs | read | low | 无 | 否 |
| asset_stats | read | low | 无 | 否 |
| list_assets | read | low | 无 | 否 |
| list_asset_plans | read | low | 无 | 否 |
| request_generate | generate | medium | 有 | 否（只建审批记录，不建任务） |
| run_generate | generate | high | 有 | 是（须先人工批准） |
| get_style_config | read | low | 无 | 否 |
| request_style_update | write | medium | 无 | 否（只建审批记录，不改配置） |
| run_style_update | write | high | 有 | 是（须先人工批准） |

`list_asset_plans` 返回项目资产定义清单（plan_id / plan_type / asset_key / status / confirmed / owner_type / owner_id / episode / seq / asset_id / version / dependencies[{slot, ref_key}]），支持 plan_type（character/scene/prop/keyframe/clip）与 status（pending/generated/reviewed/rejected）过滤及分页（limit 默认 20、最大 50），**不返回文件系统路径**（media_path/thumb_path）。

风格配置工具（三个）用于让对话读取与调整项目级风格基线，字段与取值口径见 STYLE_CONFIG.md：

- **get_style_config**：只读，返回规范化后的 config、version、saved_version、issues 与完整可选值 options，便于模型解释当前风格并给出一致取值。
- **request_style_update**：提交风格变更申请，只创建审批记录，不改配置、不影响在途任务。只提交需要修改的字段，未提交字段沿用当前值；无有效字段时返回 tool_invalid_args。side_effect 为 false 且不须审批，避免申请环节自身也需要审批造成死锁。
- **run_style_update**：执行已批准的变更，合并现有 settings 后规范化落库并写 style_config_version，返回 changed_fields 与 issues。双重门禁：requires_approval 的 approved 放行标志与审批记录状态必须为 approved，缺一不可。非法取值按规范化规则回退默认并回传问题码，不写入脏值。

### 生成申请与审批（申请-审批-执行三段分离）
- **request_generate**：提交生成申请，只创建审批记录（agent_confirmation），不建任务、不消耗算力。镜头目标（keyframe/clip/audio/bgm/sfx）须给 `shot_id`；实体目标（character/scene/prop 公共资产）须给 `entity_id`。同会话同入参重复申请复用同一条待审记录（幂等）；同镜头/同实体已有在途任务时拒绝（`job_in_flight`）。
- **制作阶段状态机门禁**：生成目标不得早于其所属制作阶段，阶段判定以 `derive_progress`（ARCHITECTURE.md §4.1 的 12 阶段）为唯一权威。申请侧（request_generate）阶段不足时快速失败（`stage_gate_blocked`），不建审批记录；执行侧（run_generate）纵深防御再次校验，防止审批通过后项目阶段回退（如剧本变更回到 estimate）仍执行生成。阈值与 API 通道同等强度：keyframe 须公共资产齐全（assets_review 阶段起，审核未通过/资产缺失/版本无效等细粒度阻断由 D-23 统一资产就绪门禁给出精确错误码）；clip/audio 须公共资产审核通过（keyframes 阶段起，单镜细粒度由 D-13 逐镜校验）；character/prop 须 characters 阶段起，scene 须 scenes 阶段起。bgm/sfx 不在 12 阶段状态机内，不做阶段门禁。D-13/D-23 人工审核闸口保持不变，不可跳过。
- **run_generate**：执行已批准的申请，创建生成任务入队。幂等键 `agc:{confirmation_id}`，重复执行复用同一任务。门禁与 API 通道同等校验，不可绕过：
  - keyframe：D-23 统一资产就绪门禁（必需资产齐全/有效/审核通过）；
  - clip：D-13 静图先行闸口（分镜图须人工确认，`keyframe_not_confirmed`）；
  - 实体目标：走 keyframe 图像工作流，任务参数注入归属键（`__character_id__`/`__scene_id__`/`__prop_id__`），生成后回退项目审核状态为 pending（D-23）。
- **参数组装与 API 通道对齐**：镜头目标 prompt 兜底 `shot.image_prompt`/`shot.video_prompt`，audio 走 `tts_text`（台词）；clip 含跨集连续性首帧注入、镜间尾帧依赖注入（声明必需而缺失时 `prev_frame_missing` 拦截）、@引用参考图注入；实体目标空 prompt 时按实体属性用纯净化模板构造，工作流绑定 negative 参数时注入负面约束。注入明细写入任务 `input_snapshot` 供追溯。

`list_assets` 返回项目资产清单（asset_id / asset_type / name / version / validity / quality_status / has_file / has_thumb / source_job_id / locked / ref_count / generated_at），以及 `count`（本次返回条数）与 `offset`（本次起始偏移），**不返回文件系统路径**；前端据 `asset_id` 通过 `GET /api/assets/{asset_id}/file?thumb=true` 取缩略图。

排序规则：按产出该资产的 `Job.created_at` 倒序（`Asset` 无自身时间戳字段），无关联任务的资产排在最后，`generated_at` 即该任务的创建时间（无关联任务时为 null）。参数 `limit` 默认 8、取值 1–30；`offset` 需大于等于 0，用于分页。

### 资产引用（asset_refs）

assistant 消息 payload 中的 `asset_refs` 为本轮可展示的资产引用数组（每项含 asset_id / asset_type / name / has_thumb / validity，最多 6 条），供前端在助手气泡内渲染缩略图；视频类（clip/video）前端降级为链接卡片。该字段为空数组时前端不渲染图片区（进展类问答即为空数组）。

前端渲染约定：
- 气泡内图片只请求 `GET /api/assets/{asset_id}/file?thumb=true` 并加 `loading=lazy`，不直接加载原图；
- 点击缩略图进入预览层查看原图（naive-ui `NImage`，`preview-src` 指向不带 `thumb` 的原图 URL，支持缩放/旋转/下载），不再新开标签页直接加载数 MB 原图；
- 缩略图加载失败（资产已失效或被清理）时前端记录失败 asset_id 并隐藏该项，避免历史消息出现破图。

- 入参经 Pydantic Schema 校验（model_validate），校验失败返回 `tool_invalid_args`。
- `requires_approval=true` 的工具未获批时返回 `approval_required`，不产生副作用（当前仅 run_generate 需审批，经审批面板批准后由 execute 接口以 approved=True 调用）。

### Agent 任务/计划/审批表（B 档）
- `agent_task`：结构化意图识别结果落库（intent/intent_goal/risk_level/confidence/success_criteria/status）。
- `agent_plan_step`：任务执行计划步骤（step_type=tool_call/clarification/reply，模型提议、代码裁决）。
- `agent_confirmation`：高风险工具审批门禁（pending/approved/rejected）。

## 13. 事件流

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
