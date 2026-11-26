# Agent Runtime 升级 · 交接说明（HANDOFF）

> 本文件面向「接手这项工作的下一位编码 Agent / 用户」。目标是让接手者在 10 分钟内恢复现场，
> 不需要重读全部历史对话。最后更新：2026-11-27（Phase B 完成，待进入 Phase C）。
> **本文件不记录任何密码、密钥或登录凭据。**

## 1. 从哪读起

按顺序读：

1. `docs/agent-runtime/plan/final-plan.md` —— 最终方案；**§五 硬性约束**必须逐条遵守。
2. `docs/agent-runtime/plan/README.md` —— 阶段顺序与门禁。
3. 当前阶段的提示词文件 `docs/agent-runtime/plan/phase-<x>-agent-prompts.md` 开头的「〇、通用上下文」。
4. `docs/agent-runtime/implementation-plan.md` —— 顶部「当前进度」+ 各任务勾选与提交号。
5. `docs/DECISIONS.md` §6「Agent 自主决定（待用户复核）」—— 所有 Agent 自行拍板的决定。

## 2. 工作方式

- 仓库：`/home/ubuntu/director-lab`，后端在 `backend/`（依赖用 `uv` / 已有 `.venv`），前端 `frontend/`。
- 直接在 `main` 上工作；**每个任务一次本地提交**，提交信息以任务号开头，正文含「改了什么 / 如何验证 / 遗留问题」。
- **不推送、不开 PR、不建 worktree。**
- 测试统一用 `cd backend && .venv/bin/python -m pytest -q ...`（非登录 shell 里 `uv` 可能不在 PATH）。
- 每个任务完成时，在同一次提交里更新 `docs/agent-runtime/implementation-plan.md`（勾选 + 提交号）。
- 每个阶段结束：填写该阶段「阶段完成记录」，并按约定格式汇报（阶段 / 提交列表 / 门禁 / 全量 pytest 对比 / 偏离与遗留 / 真实库核对 / 工作区 / 下一步）。

## 3. 自主执行规则

遇到问题自己解决：测试修不好就找根因；提示词与代码不符时以代码为准，选最保守、最符合 final-plan 原则的方案；
必要时可做最小范围外修改；迁移冲突要修到临时库上「升级 → 回退 → 再升级」都干净；
全量失败数不得多于基线，**不得删除、跳过或放宽已有测试**。

### 只有这 4 种情况必须停下

1. 真实库业务数据可能被修改。
2. 需要重启 / 停止 / 重载线上服务。
3. 需要推送到远端。
4. 需要修改服务器配置（`.env`、`data/runtime_settings.json` 等）。

## 4. 环境注意事项

- 线上服务 `director-lab-backend.service` **从该目录运行**（无 `--reload`，模块首次调用读磁盘代码；服务重启时 `app/main.py` 会自动 `alembic upgrade head`）。**不要重启服务**；`main` 上每次提交都必须能导入、测试通过。
- 新迁移只加可空列或新表，必须带 downgrade。
- 真实库 `backend/data/director_lab.db`：**绝不对它执行迁移或写操作**。核对方式只读：
  - `alembic_version` 的版本号；
  - `SELECT sql FROM sqlite_master ORDER BY name` 的结果哈希；
  - 主要业务表行数。
  - 主文件哈希无效（服务在写 WAL）。
- 测试只用临时库；A00/A00b 的守卫（`tests/test_test_isolation.py`、`tests/test_no_real_db_in_tests.py`、`tests/conftest.py`）不得放宽。
- ComfyUI 配置已被用户去掉，LLM 配置仍在；环境有 SOCKS 代理。
- 不打开 `agent_runtime_chat_enabled` 或全局审核开关；需要临时起前后端时用临时数据目录与非默认端口，结束后关闭。

## 5. 用户已做的决定

| 决策 | 内容 |
|---|---|
| 审核门禁 | 项目级开关（`project.settings["review_policy"]`），新项目默认开启、存量默认关闭 |
| Planner 是否用 LLM | 不用；计划由代码按真实进度生成，LLM 只解析目标 |
| `/api/agent/loop/run` | 保留为调试接口（默认关闭），前端不暴露 |
| 多用户权限 | 延后 |
| F5 | 照常运行真实 LLM 评测脚本，LLM 调用 ≤200 次，结果写入 `docs/agent-runtime/eval/` |
| F10 | 只做不依赖 ComfyUI 的部分；其余标「待恢复 ComfyUI 后执行」；Windows exe 打包标「待用户执行」；开关默认值保持关闭，只在 DECISIONS 写建议值 |
| E 阶段 | 做不到的截图 / 人工验收项标「待人工验收」 |
| F6 | 写好 `backend-checks.yml` 并在本地跑通；README 不加徽章 |

## 6. 中断后如何恢复

1. `cd /home/ubuntu/director-lab && git status --short && git log --oneline -20`。
2. 看 `docs/agent-runtime/implementation-plan.md` 顶部「当前进度」：当前阶段、下一个任务、最近提交、最近全量 pytest。
3. 若有未提交改动：先完整审阅 diff（是否属于某个任务、是否完整），补完或按任务提交，**不要直接丢弃**。
4. 若发现「另一个会话仍在写仓库」（文件 mtime 持续变化、有 pytest 进程不是自己起的）：
   先观察（每 60s 检查 `git status --porcelain` 与 mtime，连续 3 次无变化且无其 pytest 进程），再接手；
   超过 10 分钟仍在写，才考虑终止本机上那个会话的进程树（服务器上不要 kill 无关进程）。
5. 恢复后先跑一次相关测试再继续：
   `cd backend && .venv/bin/python -m pytest -q tests/test_agent_runtime_*.py`。

## 7. 当前状态

### Phase B · 最小可用版本

- 状态：**已完成**（B0 → B11）
- 关键提交：B0 `f3d97e4`、B1 `4f75a3a`、B2 `66c3cd6`、B3 `e7ad13d`、B4 `8ffa690`、B5 `7ca4668`、
  B6 `76c6795`、B7 `84a47fb`、B8 `84cb666`、B9 `a75bafa`、B10 `0a25415`、B11（本提交）
- 门禁：`tests/test_agent_phase_b_e2e.py` 7 个场景全绿；Phase A 的 `test_p0_9_resume_has_caller`
  已从 `xfail` 转正并通过（断言固化「步骤标志位 + 兜底扫描」的真实实现）
- 全量 pytest：1625 passed / 0 failed（Phase A 末 1560 passed / 1 xfailed）
- 开关：`agent_runtime_chat_enabled` 默认 **false** 且线上未打开；关闭时 `/chat` 行为由黄金快照
  `tests/fixtures/chat_baseline_flag_off.json` 固化。**Phase C 完成前不要打开。**
- 遗留问题清单见 `implementation-plan.md` 的「Phase B 遗留问题汇总」（7 条，作为 Phase C/D 输入）。

#### B5 部署注意事项（重要）

- 线上 `director-lab-backend.service` 用 `uvicorn app.main:app` 启动，systemd 单元**未设置**
  `DLAB_WORKER_EMBEDDED`；`app/core/config.py` 默认 `worker_embedded=True`，
  即 **Worker 内嵌在 API 进程**，`run_worker` 随 lifespan 一起启动。
- 因此 B5 新增的 `agent-resume` 守护线程（`app/services/agent_resume.py`）**要等服务下次重启才生效**。
  本轮不重启服务（属于「必须停下」的 4 种情况之一），线程是否启动由部署方在下次重启时自然接管。
- `agent_resume_enabled` 默认 `True`；但 `agent_runtime_chat_enabled` 默认 `False`，
  `/chat` 不会创建 Goal，因此线程即使运行也遇不到任何带标记的步骤——
  `tests/test_agent_phase_b_resume_worker.py::test_chat_flag_off_means_no_marked_steps` 证明这一点。
- 线程每 2 秒 `process_marked`，每 30 秒 `sweep`；异常只记日志，不会让调度主循环退出；
  停止时 `run_worker` 置 stop_event 并 join ≤ 5 秒。

### Phase A · 止血与正确性

- 状态：**已完成**（A00 → A12b；A12b 为回归汇总与文档收尾）
- 关键提交：A00 `0eaac87`、A0 `63b624a`、A12a `f99b886`、A1 `5a54196`、A2 `654a785`、A3 `2223d14`、A4 `4c039a4`、A5 `a6b2348`、A6 `cace2f0`、A7 `e8c2f38`、A8 `8131b61`、A9 `ca0fe1c`、A10 `5e335b1`、A11 `3cc9490`、A12b（本提交）
- 门禁：`tests/test_agent_runtime_*.py` + `tests/test_agent_loop_api.py` 101 passed / 1 xfailed（P0-9 占位）
- 全量 pytest：1560 passed / 1 xfailed / 0 failed（基线 1445 passed / 1 failed）

### Phase C–F

- 状态：待开始（以 `implementation-plan.md` 顶部「当前进度」为准）
- 下一个任务：Phase C · C0「统一工具目录（三套注册表的门面）」
- 进入 Phase C 前先完整重读 `plan/phase-c-agent-prompts.md` 的「〇、通用上下文」与 `plan/final-plan.md` §五 硬性约束。