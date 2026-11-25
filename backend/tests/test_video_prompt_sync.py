'''P3-5：video_prompt_first 与 video_prompt_body 同步演进守护。

背景（05 计划 P3-5、03 文档 C-01）：
两份模板文本相似度 0.769，差异全部落在 H3 首镜/续镜的参考图职责上。
共享的 12 条规则（画面一致性、禁止词、声音与输出约定）需要长期同步演进，
目前只能靠人工记得两边都改，缺少自动化守护。

设计原则：
1. 共享规则断言『两边都必须有』，任一模板删除或改写即失败；
2. H3 分叉职责断言『两边必须不同』，误把两份模板合并即失败；
3. 纯文本约束守护，不触碰运行时行为，不改变模型输入。

约束来源（H3 视频规则）：首镜由当前分镜图承担构图与身份基准；
后续镜 Picture 1 为上一镜尾帧（仅连续性，不得替换人物身份），
Picture 2 为当前分镜图。
'''

from app.services.prompt_store import get_prompt

# 两份模板必须同时存在的共享规则（2026-11-25 运行时直读核对，12 条全部命中）
SHARED_RULES = [
    '任务：Ref2VA 第 {episode} 集第 {seq} 镜',
    '剧情与动作：',
    '本镜头剧情：{shot_purpose}',
    '人物动作：{beats}',
    '镜头运动：{camera_movement}',
    '必须保持：',
    '1. 人物面部特征、发型、服装与配饰一致',
    '2. 场景布局与光线色调连续',
    '3. 道具位置与持有状态连续',
    '禁止：换脸、服装跳变、道具消失、多余肢体、文字水印、上一镜画面叠加、双重曝光、叠化、半透明虚影/残影/重影。',
    '对白与声音：{sound}',
    '输出：时长 {duration} 秒，按项目画幅，高质量输出。',
]

# 身份约束在两份模板中主语不同（参考图 / 两张参考图），共享核心句式
IDENTITY_CORE = '中的同一人物必须保持面部特征、发型、服装、体型与配饰一致，不得无故更换外观。'


def test_shared_rules_present_in_both():
    '''12 条共享规则必须同时存在于首镜与续镜模板。'''
    first = get_prompt('video_prompt_first')
    body = get_prompt('video_prompt_body')
    missing = []
    for rule in SHARED_RULES:
        if rule not in first:
            missing.append('video_prompt_first 缺少共享规则：' + rule[:30])
        if rule not in body:
            missing.append('video_prompt_body 缺少共享规则：' + rule[:30])
    assert not missing, '共享规则漂移：\n' + '\n'.join(missing)


def test_identity_constraint_present_in_both():
    '''身份约束的共享核心句式必须同时存在（主语差异属 H3 必要分叉）。'''
    first = get_prompt('video_prompt_first')
    body = get_prompt('video_prompt_body')
    assert IDENTITY_CORE in first, '首镜缺少身份约束核心句式'
    assert IDENTITY_CORE in body, '续镜缺少身份约束核心句式'
    assert '【身份约束】' in first, '首镜缺少身份约束段标题'
    assert '【身份约束】' in body, '续镜缺少身份约束段标题'


def test_first_shot_has_single_reference():
    '''首镜只有当前分镜图一张参考图，不得出现 Picture 2 槽位。'''
    first = get_prompt('video_prompt_first')
    assert 'Picture 1>：当前镜头分镜图' in first, '首镜 Picture 1 必须是当前镜头分镜图'
    assert 'Picture 2' not in first, '首镜不应出现 Picture 2（H3：首镜没有上一镜尾帧）'


def test_body_shot_has_two_reference_roles():
    '''续镜必须有两张参考图，且职责不可对调。'''
    body = get_prompt('video_prompt_body')
    assert 'Picture 1>：上一镜头尾帧' in body, '续镜 Picture 1 必须是上一镜头尾帧'
    assert 'Picture 2>：当前镜头分镜图' in body, '续镜 Picture 2 必须是当前镜头分镜图'
    # 尾帧只提供连续性，禁止被当作首帧复现（11-14 H3 修复的核心禁止词）
    for word in ['禁止直接复现', '叠加', '双重曝光', '叠化']:
        assert word in body, '续镜缺少尾帧禁止词：' + word


def test_templates_not_merged():
    '''两份模板必须是独立文本：若被误合并，H3 首镜/续镜职责将无法表达。'''
    assert get_prompt('video_prompt_first') != get_prompt('video_prompt_body'), \
        'video_prompt_first 与 video_prompt_body 被合并，H3 参考图职责会失效'
