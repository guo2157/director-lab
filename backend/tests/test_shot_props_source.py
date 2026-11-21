"""两段式改造后的道具关系来源测试。

背景：
改造前，`prompt_compiler._shot_props` 从 `shot.image_prompt` 里正则提取 @道具名。
两段式改造（LLM 只出结构化事实、提示词由模板渲染）后 image_prompt 内容变了
甚至可能由模板生成，靠正则反推道具关系不再可靠——道具参考图会静默全部丢失。

`Shot.props` 是解析阶段就落库的结构化字段（实测库中为 ["帆布包"]），
应作为权威来源；提示词正则只作历史数据兜底。
"""
from __future__ import annotations

import pytest


class _FakeShot:
    def __init__(self, props=None, image_prompt=None):
        self.props = props
        self.image_prompt = image_prompt


class _FakeProp:
    def __init__(self, name):
        self.name = name


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, prop_names):
        self._props = [_FakeProp(n) for n in prop_names]

    def query(self, *a, **k):
        return _FakeQuery(self._props)


def _shot_props(db, shot, project_id="prj_x"):
    from app.services.prompt_compiler import _shot_props

    return _shot_props(db, shot, project_id)


def test_structured_props_field_is_authoritative():
    """结构化字段优先：即使 image_prompt 里没有任何 @引用，也要能取到道具。"""
    db = _FakeDB(["帆布包", "冰可乐杯", "草帽"])
    shot = _FakeShot(props=["帆布包"], image_prompt="一段完全不含@引用的模板渲染文本")
    got = {p.name for p in _shot_props(db, shot)}
    assert got == {"帆布包"}


def test_structured_props_handles_multiple():
    db = _FakeDB(["帆布包", "冰可乐杯", "草帽"])
    shot = _FakeShot(props=["帆布包", "草帽"])
    got = {p.name for p in _shot_props(db, shot)}
    assert got == {"帆布包", "草帽"}


def test_json_string_props_is_parsed():
    """历史数据可能落成 JSON 字符串而非 list。"""
    db = _FakeDB(["帆布包", "冰可乐杯"])
    shot = _FakeShot(props='["冰可乐杯"]')
    got = {p.name for p in _shot_props(db, shot)}
    assert got == {"冰可乐杯"}


def test_comma_string_props_is_parsed():
    db = _FakeDB(["帆布包", "冰可乐杯"])
    shot = _FakeShot(props="帆布包,冰可乐杯")
    got = {p.name for p in _shot_props(db, shot)}
    assert got == {"帆布包", "冰可乐杯"}


def test_falls_back_to_prompt_refs_when_props_empty():
    """结构化字段为空时，退回提示词 @引用提取（历史数据兼容）。"""
    db = _FakeDB(["帆布包", "冰可乐杯"])
    shot = _FakeShot(props=None, image_prompt="角色手持 @冰可乐杯 站在门口")
    got = {p.name for p in _shot_props(db, shot)}
    assert got == {"冰可乐杯"}


def test_returns_empty_when_both_empty():
    db = _FakeDB(["帆布包"])
    shot = _FakeShot(props=None, image_prompt="")
    assert _shot_props(db, shot) == []


def test_unknown_prop_name_is_ignored():
    """结构化字段里有、但项目内没这个道具记录时不能崩，也不能凭空造。"""
    db = _FakeDB(["帆布包"])
    shot = _FakeShot(props=["不存在的道具"])
    assert _shot_props(db, shot) == []


def test_no_project_id_returns_empty():
    db = _FakeDB(["帆布包"])
    shot = _FakeShot(props=["帆布包"])
    assert _shot_props(db, shot, project_id="") == []
