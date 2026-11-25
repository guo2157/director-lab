"""提示词注册表（P2-1）：统一元数据与内容指纹。

设计约定：
- 代码内只读元数据表，不新建数据库表（模板元数据属代码资产）
- content_hash 由 prompt_store 当前生效内容实时计算（外置优先、内置回退）
- input_vars 从模板文本动态解析，避免与实际占位符漂移；
  注意 JSON 示例中的花括号也会被解析出字段名，运行时是否 format 由调用方决定
- 注册表与 DEFAULT_PROMPTS 的一致性由 test_prompt_registry.py 强制
- content_hash 经 CompiledPrompt 写入 job.input_snapshot 供历史任务追溯
"""
from __future__ import annotations

import hashlib
import string

from app.services import prompt_store

TASK_SCRIPT_WRITING = 'script_writing'
TASK_SCRIPT_EVALUATION = 'script_evaluation'
TASK_SCRIPT_OUTLINE = 'script_outline'
TASK_SCRIPT_PARSE = 'script_parse'
TASK_ASSET_PROMPT = 'asset_prompt'
TASK_ASSET_IMAGE = 'asset_image'
TASK_SCENE_PLATE = 'scene_plate'
TASK_STORYBOARD_IMAGE = 'storyboard_image'
TASK_SHOT_VIDEO = 'shot_video'
TASK_AGENT = 'agent'
TASK_QC = 'qc'
TASK_SESSION_TITLE = 'session_title'
TASK_VOICE_PROFILE = 'voice_profile'
TASK_EPISODE_WRITING = 'episode_writing'

# prompt_id: (task_type, version, output_format, model_compatibility)
PROMPT_REGISTRY: dict[str, tuple[str, str, str, str]] = {
    'script_create_system': (TASK_SCRIPT_WRITING, '1.0', 'text', 'any'),
    'script_expand_system': (TASK_SCRIPT_WRITING, '1.0', 'text', 'any'),
    'script_normalize_system': (TASK_SCRIPT_WRITING, '1.0', 'text', 'any'),
    'script_evaluation_system': (TASK_SCRIPT_EVALUATION, '2.0', 'json', 'json_capable'),
    'script_outline_system': (TASK_SCRIPT_OUTLINE, '1.0', 'json', 'json_capable'),
    'parse_system': (TASK_SCRIPT_PARSE, '1.2', 'json', 'json_capable'),
    'asset_character': (TASK_ASSET_PROMPT, '2.1', 'text', 'any'),
    'asset_scene': (TASK_ASSET_PROMPT, '2.1', 'text', 'any'),
    'asset_prop': (TASK_ASSET_PROMPT, '2.1', 'text', 'any'),
    'compile_character_asset': (TASK_ASSET_IMAGE, '2', 'text', 'any'),
    'compile_scene_asset': (TASK_ASSET_IMAGE, '2', 'text', 'any'),
    'compile_prop_asset': (TASK_ASSET_IMAGE, '2', 'text', 'any'),
    'scene_plate_compiler': (TASK_SCENE_PLATE, '1.0', 'json', 'json_capable'),
    'image_prompt': (TASK_STORYBOARD_IMAGE, '2.0', 'text', 'any'),
    'compile_storyboard_image': (TASK_STORYBOARD_IMAGE, '3', 'text', 'any'),
    'video_prompt_first': (TASK_SHOT_VIDEO, '1.1', 'text', 'any'),
    'video_prompt_body': (TASK_SHOT_VIDEO, '1.0', 'text', 'any'),
    'compile_shot_video': (TASK_SHOT_VIDEO, '4', 'text', 'any'),
    'director_system': (TASK_AGENT, '1.0', 'text', 'any'),
    'agent_intent_system': (TASK_AGENT, '1.0', 'json', 'json_capable'),
    'agent_session_summary': (TASK_AGENT, '1.0', 'text', 'any'),
    'qc_keyframe_prompt': (TASK_QC, '1.0', 'json', 'json_capable'),
    'qc_clip_prompt': (TASK_QC, '1.0', 'json', 'json_capable'),
    'session_title_system': (TASK_SESSION_TITLE, '1.0', 'text', 'any'),
    'session_title_naming': (TASK_SESSION_TITLE, '1.0', 'text', 'any'),
    'voice_profile_system': (TASK_VOICE_PROFILE, '1.0', 'json', 'json_capable'),
    'episode_continue_system': (TASK_EPISODE_WRITING, '1.0', 'json', 'json_capable'),
}


def template_input_vars(prompt_id: str) -> list[str]:
    """从模板文本动态解析 format 占位符（空列表表示数据经 user 消息注入）。"""
    try:
        content = prompt_store.get_prompt(prompt_id)
    except (FileNotFoundError, ValueError):
        return []
    fields: list[str] = []
    for _, field_name, _, _ in string.Formatter().parse(content):
        if field_name and field_name not in fields:
            fields.append(field_name)
    return fields


def template_content_hash(prompt_id: str, *, short: bool = True) -> str:
    """当前生效模板内容的 sha256 指纹（外置优先、内置回退）；未知模板返回空串。"""
    try:
        content = prompt_store.get_prompt(prompt_id)
    except (FileNotFoundError, ValueError):
        return ''
    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
    return digest[:12] if short else digest


def template_meta(prompt_id: str) -> dict | None:
    """返回模板元数据副本（含 template_path / input_vars / content_hash）；未注册返回 None。"""
    entry = PROMPT_REGISTRY.get(prompt_id)
    if entry is None:
        return None
    task_type, version, output_format, model_compatibility = entry
    return {
        'prompt_id': prompt_id,
        'task_type': task_type,
        'version': version,
        'template_path': 'workspace/prompts/' + prompt_id + '.md',
        'model_compatibility': model_compatibility,
        'input_vars': template_input_vars(prompt_id),
        'output_format': output_format,
        'content_hash': template_content_hash(prompt_id),
        'enabled': True,
    }


def registry_snapshot() -> list[dict]:
    """全量注册表快照（含实时 content_hash），供调试与审计使用。"""
    prompt_store.ensure_registered()
    rows: list[dict] = []
    for prompt_id in sorted(PROMPT_REGISTRY):
        meta = template_meta(prompt_id) or {}
        meta['registered'] = prompt_id in prompt_store.DEFAULT_PROMPTS
        meta['externalized'] = (prompt_store.PROMPT_DIR / (prompt_id + '.md')).exists()
        rows.append(meta)
    return rows


def validate_registry() -> list[str]:
    """校验注册表完整性：未注册模板、内容为空。返回问题列表（空即合格）。"""
    prompt_store.ensure_registered()
    problems: list[str] = []
    for prompt_id in sorted(PROMPT_REGISTRY):
        if prompt_id not in prompt_store.DEFAULT_PROMPTS:
            problems.append('registry_template_not_registered: ' + prompt_id)
            continue
        if not template_content_hash(prompt_id, short=False):
            problems.append('registry_template_empty: ' + prompt_id)
    return problems
