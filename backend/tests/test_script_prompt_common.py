'''P3-1 方案 2：script_create_system 与 script_expand_system 公共规范一致性守护。

背景（05 计划 P3-1、02 文档 Q-01、03 文档 C-02）：
两份模板文本相似度 0.616，共享 4 条通用剧本输出规范，
目前靠人工记得两边都改，存在双处维护成本与漂移风险。

为何选方案 2 而非方案 1（抽公共段）：
抽公共段会改变两份模板的实际文本顺序与边界，进而改变模型输入，
可能影响剧本生成风格。按硬约束 H-8（不把推测写成已验证事实）
与 06 文档 A/B 基线要求，必须先有 A/B 数据才能做方案 1。
方案 2 是零运行时风险的等价守护。

实测口径（2026-11-25 运行时直读核对）：
4 条子串在两份模板中同时命中；『人物名一致』两条措辞不同
（create 含『不使用代称；同一地点使用统一场景名』，
expand 为『与场景名全剧一致稳定』），属语义重叠而非文本重复，
因此分别断言各自存在，不强行统一措辞。
'''

import pytest

from app.services.prompt_store import get_prompt

# 两份模板必须同时存在的通用规范（实测 4 条全部命中）
SHARED_RULES = [
    '输出为标准剧本格式：场次标题（时间/地点）、场景描述、人物动作、对白（说话人+台词）、必要的旁白与声音提示。',
    '每集以「第X集」明确分隔；',
    '只输出剧本正文，不输出',
    '遵守用户给定的题材、受众、画风、集数、每集时长、语言与对白风格、内容尺度',
]

# 人物名一致性：语义相同、措辞不同，分别守护
NAMING_RULES = {
    'script_create_system': '人物名称全剧保持一致且稳定',
    'script_expand_system': '人物名称与场景名全剧一致稳定',
}


def _prompts():
    return {
        'script_create_system': get_prompt('script_create_system'),
        'script_expand_system': get_prompt('script_expand_system'),
    }


def test_shared_rules_present_in_both():
    '''4 条通用剧本规范必须同时存在于创作与扩写模板。'''
    prompts = _prompts()
    missing = []
    for name, text in prompts.items():
        for rule in SHARED_RULES:
            if rule not in text:
                missing.append(name + ' 缺少通用规范：' + rule[:28])
    assert not missing, '通用规范漂移：\n' + '\n'.join(missing)


def test_naming_consistency_present_in_both():
    '''人物名一致性约束必须同时存在（措辞差异属预期，不强行统一）。'''
    prompts = _prompts()
    for name, rule in NAMING_RULES.items():
        assert rule in prompts[name], name + ' 缺少人物名一致性约束：' + rule


def test_role_boundary_distinct():
    '''两份模板职责边界必须可区分：一份写创作、一份写扩写。'''
    create = get_prompt('script_create_system')
    expand = get_prompt('script_expand_system')
    assert '创作' in create, '创作模板缺少创作语义'
    assert '扩写' in expand, '扩写模板缺少扩写语义'
    assert create != expand, 'create 与 expand 被合并，创作/扩写职责会失效'


def test_creation_specific_rules_intact():
    '''创作独有规则（四节拍）与扩写独有规则（忠实梗概）不得互相串入。'''
    create = get_prompt('script_create_system')
    expand = get_prompt('script_expand_system')
    assert '四节拍' in create, '创作模板缺少四节拍要求'
    assert '四节拍' not in expand, '扩写模板不应包含创作专属的四节拍要求'
    assert '忠实保留梗概中的核心事件' in expand, '扩写模板缺少忠实梗概约束'
    assert '忠实保留梗概中的核心事件' not in create, '创作模板不应包含扩写专属的忠实梗概约束'


@pytest.mark.parametrize('name', ['script_create_system', 'script_expand_system'])
def test_both_templates_nonempty(name):
    '''冒烟：两份模板均可读取且非空，防止 get_prompt 回退异常导致空断言。'''
    text = get_prompt(name)
    assert text and len(text) > 100, name + ' 内容异常（可能读取失败）'
