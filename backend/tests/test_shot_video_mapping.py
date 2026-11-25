'''P3-4：编译模板占位符必须被映射字典完整覆盖。

背景（05 计划 P3-4、02 文档 Q-04）：
compile_shot_video 有 24 个占位符、仅 11 行，占位符密度全库最高，
缺省值完全依赖编译侧映射。若模板新增占位符而映射未同步，
render_template 会在运行时抛 prompt_template_missing_field，
只有真正跑到该编译分支才会暴露。本测试把该约束前移到静态检查。

两种映射构造形态（均已直读确认）：
1. 内联赋值：compile_shot_video / compile_storyboard_image 在函数内写 mapping = {...}
2. 辅助函数返回：compile_asset_image 依赖 _character_mapping / _scene_mapping / _prop_mapping 的 return {...}

实现说明（规避 06 文档 R-05）：
不使用 inspect.getsource —— 它依赖 linecache，模块导入后若磁盘文件被改写
会用旧行号读错函数体（本轮全量回归已实测到该误报）。
这里用 ast.parse 每次直读磁盘文本，不依赖行号缓存。

断言口径：
1. 模板每个占位符都必须由映射提供（缺失即失败）；
2. 映射允许有模板未取用的额外键，不视为错误；
3. 映射的键必须是字符串字面量，否则静态检查会失效。
'''

import ast
from pathlib import Path

import pytest

from app.services.prompt_registry import template_input_vars

COMPILER_PATH = Path(__file__).resolve().parents[1] / 'app' / 'services' / 'prompt_compiler.py'


def _parse_file():
    return ast.parse(COMPILER_PATH.read_text(encoding='utf-8'))


def _find_func(func_name):
    for node in ast.walk(_parse_file()):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return node
    raise AssertionError('未找到函数 ' + func_name)


def _assign_mapping_keys(func_name):
    '''解析函数体内 mapping = {...} 赋值语句的键集合。'''
    func = _find_func(func_name)
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == 'mapping':
            return [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
    raise AssertionError('未在 ' + func_name + ' 中找到 mapping 字典字面量')


def _return_mapping_keys(func_name):
    '''解析函数体 return {...} 字典字面量的键集合。'''
    func = _find_func(func_name)
    for node in ast.walk(func):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            return [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
    raise AssertionError('未在 ' + func_name + ' 中找到 return 字典字面量')


def _assert_covers(keys, template_id, scope):
    required = template_input_vars(template_id)
    missing = sorted(set(required) - set(keys))
    assert not missing, (
        scope + ' 未覆盖模板 ' + template_id + ' 的占位符：' + '、'.join(missing)
        + '（运行时会抛 prompt_template_missing_field）'
    )


def test_shot_video_mapping_covers_template():
    '''compile_shot_video 的 mapping 必须覆盖模板全部占位符。'''
    _assert_covers(_assign_mapping_keys('compile_shot_video'), 'compile_shot_video',
                   'compile_shot_video.mapping')


def test_storyboard_image_mapping_covers_template():
    '''compile_storyboard_image 的 mapping 必须覆盖模板全部占位符。'''
    _assert_covers(_assign_mapping_keys('compile_storyboard_image'),
                   'compile_storyboard_image', 'compile_storyboard_image.mapping')


@pytest.mark.parametrize('func_name,template_id', [
    ('_character_mapping', 'compile_character_asset'),
    ('_scene_mapping', 'compile_scene_asset'),
    ('_prop_mapping', 'compile_prop_asset'),
])
def test_asset_mapping_covers_template(func_name, template_id):
    '''三个资产映射辅助函数的返回值必须覆盖对应编译模板的占位符。'''
    _assert_covers(_return_mapping_keys(func_name), template_id, func_name)


def test_mapping_keys_are_string_literals():
    '''映射的键必须是字符串字面量，否则静态覆盖检查会失效。'''
    non_literal = []
    for node in ast.walk(_parse_file()):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == 'mapping':
            for k in node.value.keys:
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                    non_literal.append(ast.dump(k)[:40])
    assert not non_literal, 'mapping 存在非字符串字面量键：' + '；'.join(non_literal)
