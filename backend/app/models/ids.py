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
def episode_parse_record_id() -> str:
    return new_id("epr")


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

def episode_continuity_id() -> str:
    return new_id("ect")


def agent_session_id() -> str:
    return new_id("ags")


def agent_task_id() -> str:
    return new_id("agt")


def agent_plan_step_id() -> str:
    return new_id("aps")


def agent_confirmation_id() -> str:
    return new_id("agc")
def task_id() -> str:
    return new_id("tsk")
def task_stage_id() -> str:
    return new_id("tst")
def task_result_id() -> str:
    return new_id("tsr")


def agent_message_id() -> str:
    return new_id("agm")


def job_input_ref_id() -> str:
    return new_id("jir")


def asset_plan_id() -> str:
    return new_id("apn")


def asset_plan_dep_id() -> str:
    return new_id("apd")
def search_run_id() -> str:
    return new_id("srh")
def agent_memory_id() -> str:
    return new_id("amr")
def search_result_id() -> str:
    return new_id("srs")
def voice_profile_id() -> str:
    return new_id("vp")
def audio_segment_id() -> str:
    return new_id("aseg")
def audio_review_id() -> str:
    return new_id("arev")

def voice_library_id() -> str:
    return new_id("vl")


# --- 系列化创作（Series / Episode 叙事维度）---

def series_id() -> str:
    return new_id("srs")

def series_episode_id() -> str:
    return new_id("eps")

def series_bible_version_id() -> str:
    return new_id("sbv")

def series_script_version_id() -> str:
    return new_id("ssv")

def series_asset_id() -> str:
    return new_id("sas")

def episode_asset_ref_id() -> str:
    return new_id("ear")

def story_event_id() -> str:
    return new_id("sev")

def continuity_snapshot_id() -> str:
    return new_id("csn")

def continuity_proposal_id() -> str:
    return new_id("ccp")

def episode_context_manifest_id() -> str:
    return new_id("ecm")

def plot_thread_id() -> str:
    return new_id("plt")


# --- Agent Runtime（Phase 1）---

def agent_goal_id() -> str:
    return new_id("gol")


def agent_plan_id() -> str:
    return new_id("pln")


def agent_execution_log_id() -> str:
    return new_id("ael")
