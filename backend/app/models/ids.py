"""业务 ID 生成：AGENTS.md §4 前缀规则。

项目 prj_、场景 scn_、镜头 sht_、角色 chr_、道具 prp_、资产 ast_、任务 job_、工作流 wf_；
其余实体采用语义化短前缀。应用层生成，不使用数据库自增。
"""
import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def project_id() -> str:
    return new_id("prj")


def scene_id() -> str:
    return new_id("scn")


def shot_id() -> str:
    return new_id("sht")


def character_id() -> str:
    return new_id("chr")


def prop_id() -> str:
    return new_id("prp")


def asset_id() -> str:
    return new_id("ast")


def job_id() -> str:
    return new_id("job")


def workflow_id() -> str:
    return new_id("wf")


def script_version_id() -> str:
    return new_id("sv")


def script_estimate_id() -> str:
    return new_id("est")


def parse_version_id() -> str:
    return new_id("pv")


def scene_episode_id() -> str:
    return new_id("sep")


def asset_ref_id() -> str:
    return new_id("aref")


def dependency_ref_id() -> str:
    return new_id("dep")


def upload_record_id() -> str:
    return new_id("upl")


def composition_id() -> str:
    return new_id("cmp")


def export_id() -> str:
    return new_id("exp")
