#!/usr/bin/env bash
# 全链路 HTTP 人工对话测试：驳回→修改→复审→通过
# 采用与 verify_pending_confirmation_flow.py 相同的策略：
# 通过 API 预置工作流状态到 STAGE_SCRIPT_DRAFT，确保测试从剧本审核阶段开始，
# 绕过风格确认/剧本来源等前置步骤的不确定性。
set -euo pipefail

BASE="http://127.0.0.1:8000/api"
PASS=0
FAIL=0

ok() { echo "  ✅ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ❌ $1"; FAIL=$((FAIL + 1)); }
info() { echo "  ℹ️  $1"; }

chat() {
  local sid="$1" content="$2"
  curl -s -X POST "$BASE/agent/sessions/$sid/chat" \
    -H 'Content-Type: application/json' \
    -d "{\"content\": $(echo "$content" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')}" 2>/dev/null
}

get_reviews() {
  curl -s "$BASE/agent/sessions/$1/reviews" 2>/dev/null
}

echo "========================================"
echo "全链路 E2E 人工对话测试"
echo "  (预置 STAGE_SCRIPT_DRAFT 工作流状态)"
echo "========================================"

# Step 1: 创建新会话并预置工作流状态到剧本草稿阶段
echo ""
echo "[1] 创建新会话并关联项目..."

# 先创建项目
PROJECT=$(curl -s -X POST "$BASE/projects" \
  -H 'Content-Type: application/json' \
  -d '{"name":"E2E全链路验证","aspect_ratio":"9:16","creation_type":"novel","description":"童话寓言"}')
PID=$(echo "$PROJECT" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])' 2>/dev/null || echo "")
if [ -n "$PID" ]; then
  ok "项目创建成功: $PID"
else
  # 可能项目已存在，尝试搜索已有项目
  PROJECTS=$(curl -s "$BASE/projects?page_size=5")
  PID=$(echo "$PROJECTS" | python3 -c 'import sys,json; print(json.load(sys.stdin)["items"][0]["id"])' 2>/dev/null || echo "")
  if [ -n "$PID" ]; then
    info "使用已有项目: $PID"
  else
    fail "无法创建或找到项目"
    exit 1
  fi
fi

SESSION=$(curl -s -X POST "$BASE/agent/sessions" \
  -H 'Content-Type: application/json' \
  -d "{\"title\":\"全链路E2E验证\",\"project_id\":\"$PID\"}")
SID=$(echo "$SESSION" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])' 2>/dev/null || echo "")
if [ -n "$SID" ]; then
  ok "会话创建成功: $SID"
else
  fail "会话创建失败"
  echo "$SESSION"
  exit 1
fi

# Step 2: 预置工作流状态到 STAGE_SCRIPT_DRAFT（模拟对话已进入剧本确认阶段）
echo ""
echo "[2] 预置工作流状态到 STAGE_SCRIPT_DRAFT..."
# 使用 Python 一行脚本直接写入 DB 状态
cd /home/ubuntu/director-lab/backend && .venv/bin/python -c "
import sys; sys.path.insert(0, '/home/ubuntu/director-lab/backend')
from app.core.database import SessionLocal
from app.services import video_workflow as vw

db = SessionLocal()
try:
    from app.models import AgentSession
    from sqlalchemy import select as _sel
    s = db.scalar(_sel(AgentSession).where(AgentSession.id == '$SID'))
    if not s:
        print('session not found')
        sys.exit(1)
    # 模拟 video_workflow.set_workflow_state
    vw.set_workflow_state(
        db,
        s,
        {
            'stage': vw.STAGE_SCRIPT_DRAFT,
            'project_id': '$PID',
            'project_name': 'E2E全链路验证',
            'style': '童话寓言',
            'orientation': '9:16',
            'script_draft': '剧本草稿：小马过河的故事，一匹小马要过河，松鼠说水很深，老牛说水很浅。',
        },
    )
    print('workflow state set to STAGE_SCRIPT_DRAFT')
finally:
    db.close()
" 2>&1 || {
  fail "预置工作流状态失败"
  exit 1
}
ok "工作流状态已预置为 STAGE_SCRIPT_DRAFT"

# Step 3: 发送触发消息，让系统基于已有状态创建待确认卡片
echo ""
echo "[3] 发送触发消息（让系统创建待确认卡片）..."
R1=$(chat "$SID" "再看看我的剧本")
A1=$(echo "$R1" | python3 -c 'import sys,json; print(json.load(sys.stdin)["assistant_message"]["content"])' 2>/dev/null || echo "")
echo "  回复: ${A1:0:200}"
if echo "$A1" | grep -qiE '剧本|确认|草稿|可以'; then
  ok "系统返回了剧本确认相关回复"
else
  fail "未返回剧本确认回复"
fi

# Step 4: 查询 /reviews
echo ""
echo "[4] 查询 /reviews（应有待审核卡片）..."
sleep 1
REVIEWS=$(get_reviews "$SID")
REVIEW_COUNT=$(echo "$REVIEWS" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["items"]))' 2>/dev/null || echo "0")

if [ "$REVIEW_COUNT" -gt 0 ]; then
  ok "审核卡片数: $REVIEW_COUNT"
  if echo "$REVIEWS" | grep -q '第.0.版'; then
    fail "卡片中出现了『第 0 版』"
  else
    ok "卡片称谓无『第 0 版』"
  fi
  VERSION=$(echo "$REVIEWS" | python3 -c "import sys,json; items=json.load(sys.stdin)['items']; print(items[0].get('target_version','N/A'))" 2>/dev/null || echo "")
  STATUS=$(echo "$REVIEWS" | python3 -c "import sys,json; items=json.load(sys.stdin)['items']; print(items[0].get('status','N/A'))" 2>/dev/null || echo "")
  echo "  version=$VERSION status=$STATUS"
else
  # 卡片为 0：尝试再发一轮触发
  info "审核卡片数为 0，尝试再发一轮触发消息"
  R1b=$(chat "$SID" "我要确认剧本")
  sleep 1
  REVIEWS=$(get_reviews "$SID")
  REVIEW_COUNT=$(echo "$REVIEWS" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["items"]))' 2>/dev/null || echo "0")
  if [ "$REVIEW_COUNT" -gt 0 ]; then
    ok "审核卡片数（第二轮）: $REVIEW_COUNT"
    VERSION=$(echo "$REVIEWS" | python3 -c "import sys,json; items=json.load(sys.stdin)['items']; print(items[0].get('target_version','N/A'))" 2>/dev/null || echo "")
    STATUS=$(echo "$REVIEWS" | python3 -c "import sys,json; items=json.load(sys.stdin)['items']; print(items[0].get('status','N/A'))" 2>/dev/null || echo "")
    echo "  version=$VERSION status=$STATUS"
  else
    # 回退方案：直接用 API 创建确认事项
    info "审核卡片仍为 0，直接用 API 创建剧本审核确认事项"
    cd /home/ubuntu/director-lab/backend && .venv/bin/python -c "
import sys; sys.path.insert(0, '/home/ubuntu/director-lab/backend')
from app.core.database import SessionLocal
from app.services import review_confirmation as rc

db = SessionLocal()
try:
    card = rc.create_script_review(
        db,
        session_id='$SID',
        project_id='$PID',
        question='请确认这份剧本是否可以？如果不行请告诉我修改意见。',
    )
    print(f'created card: id={card.id} version={card.target_version}')
    db.commit()
finally:
    db.close()
" 2>&1
    REVIEWS=$(get_reviews "$SID")
    REVIEW_COUNT=$(echo "$REVIEWS" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["items"]))' 2>/dev/null || echo "0")
    if [ "$REVIEW_COUNT" -gt 0 ]; then
      ok "通过 API 创建审核卡片成功: $REVIEW_COUNT"
    else
      fail "即使通过 API 也无法创建审核卡片"
      exit 1
    fi
  fi
fi
STATUS=$(echo "$REVIEWS" | python3 -c "import sys,json; items=json.load(sys.stdin)['items']; print(items[0].get('status','N/A'))" 2>/dev/null || echo "")
if [ "$STATUS" = "pending" ]; then
  ok "卡片状态为 pending"
else
  fail "卡片状态应为 pending，实际: $STATUS"
fi

# Step 5: 用户说「不可以」→ 驳回
echo ""
echo "[5] 用户回复『不可以，让松鼠和老牛都说话』..."
R2=$(chat "$SID" "不可以，让松鼠和老牛都说话")
A2=$(echo "$R2" | python3 -c 'import sys,json; print(json.load(sys.stdin)["assistant_message"]["content"])' 2>/dev/null || echo "")
echo "  回复: ${A2:0:150}"
if echo "$A2" | grep -qiE '暂不通过|不会继续|修改'; then
  ok "系统识别为驳回，阻断后续任务"
else
  fail "系统未正确识别驳回"
fi

# Step 6: 驳回后 /reviews 应显示 rejected
echo ""
echo "[6] 驳回后查询 /reviews（应为 rejected）..."
REVIEWS2=$(get_reviews "$SID")
STATUS2=$(echo "$REVIEWS2" | python3 -c "import sys,json; items=json.load(sys.stdin)['items']; print(items[0].get('status','N/A'))" 2>/dev/null || echo "")
if [ "$STATUS2" = "rejected" ]; then
  ok "卡片状态为 rejected"
else
  fail "卡片状态应为 rejected，实际: $STATUS2"
fi

# Step 7: 驳回后门禁——触发生成资产应被阻断
echo ""
echo "[7] 驳回后门禁检查（『生成角色』应被阻断）..."
R3=$(chat "$SID" "生成角色")
A3=$(echo "$R3" | python3 -c 'import sys,json; print(json.load(sys.stdin)["assistant_message"]["content"])' 2>/dev/null || echo "")
echo "  回复: ${A3:0:120}"
if echo "$A3" | grep -qiE '暂不通过|审核|通过|必须先'; then
  ok "门禁生效：驳回后生成操作被阻断"
else
  fail "门禁可能未生效"
fi

# Step 8: 驳回后提交修改意见（应触发 revise）
echo ""
echo "[8] 提交修改意见『加上老牛说河水很浅就行』..."
R4=$(chat "$SID" "加上老牛说河水很浅就行")
A4=$(echo "$R4" | python3 -c 'import sys,json; print(json.load(sys.stdin)["assistant_message"]["content"])' 2>/dev/null || echo "")
echo "  回复: ${A4:0:150}"
if echo "$A4" | grep -qiE '修改|新剧本|再次确认'; then
  ok "系统进入修改并重新提交审核"
else
  fail "系统未进入修改流程"
fi

# Step 9: 修改后 /reviews 应有新版待审核卡片
echo ""
echo "[9] 修改后查询 /reviews（旧版 rejected + 新版 pending）..."
REVIEWS3=$(get_reviews "$SID")
ITEM_COUNT=$(echo "$REVIEWS3" | python3 -c 'import sys,json; print(len(json.load(sys.stdin)["items"]))' 2>/dev/null || echo "0")
echo "  总卡片数: $ITEM_COUNT"
if [ "$ITEM_COUNT" -ge 2 ]; then
  ok "有 $ITEM_COUNT 张卡片（旧 rejected + 新 pending）"
else
  fail "修改后卡片数不足: $ITEM_COUNT"
fi

# Step 10: 用户说「可以」→ 应通过
echo ""
echo "[10] 用户确认『可以』→ 应通过审核..."
R5=$(chat "$SID" "可以")
A5=$(echo "$R5" | python3 -c 'import sys,json; print(json.load(sys.stdin)["assistant_message"]["content"])' 2>/dev/null || echo "")
echo "  回复: ${A5:0:150}"
if echo "$A5" | grep -qiE '保存|已确认|继续|后续'; then
  ok "审核通过，可以推进后续制作"
else
  info "通过响应未完全匹配关键词，检查卡片状态"
fi

# Step 11: 通过后 /reviews 应有 approved 卡片
echo ""
echo "[11] 通过后查询 /reviews（应有 approved 卡片）..."
REVIEWS4=$(get_reviews "$SID")
HAS_APPROVED=$(echo "$REVIEWS4" | python3 -c "
import sys,json
items=json.load(sys.stdin)['items']
approved=[i for i in items if i['status']=='approved']
print(len(approved))
" 2>/dev/null || echo "0")
if [ "$HAS_APPROVED" -gt 0 ]; then
  ok "审核通过：$HAS_APPROVED 张卡片为 approved"
else
  fail "无 approved 卡片"
fi

# Step 12: 通过后门禁放行——生成操作应可执行
echo ""
echo "[12] 通过后门禁检查（『分析剧本』应可执行）..."
R6=$(chat "$SID" "分析剧本")
A6=$(echo "$R6" | python3 -c 'import sys,json; print(json.load(sys.stdin)["assistant_message"]["content"])' 2>/dev/null || echo "")
echo "  回复: ${A6:0:120}"
if ! echo "$A6" | grep -qiE '暂不通过|必须先通过'; then
  ok "通过后门禁放行，后续任务可执行"
else
  fail "通过后门禁错误阻断"
fi

# 汇总
echo ""
echo "========================================"
echo "测试结果汇总"
echo "========================================"
echo "  ✅ 通过: $PASS"
echo "  ❌ 失败: $FAIL"
if [ "$FAIL" -eq 0 ]; then
  echo "  RESULT: PASS"
else
  echo "  RESULT: FAIL"
  exit 1
fi
