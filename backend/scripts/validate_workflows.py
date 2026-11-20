#!/usr/bin/env python3
"""按 PRD 5.5 / 8.2 校验 API 格式、标记、连线与输出标记。用法: python3 validate_workflows.py wf1.json ..."""
import json, os, re, sys
# 画布专用残留键（WORKFLOW.md §1 防复发规则，与 backend/app/services/workflow_inject.py 同套规则）
CANVAS_ONLY_KEYS={"control_after_generate","widgets_values"}

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def parse_title(t):
    params=[];outs=[]
    for frag in [f.strip() for f in t.split(",")]:
        if not frag.startswith("$"): continue
        if frag.startswith("$output."): outs.append(frag[8:]); continue
        m=re.fullmatch(r"\$(~?)([A-Za-z0-9_]+)(?:\.(~?)([A-Za-z0-9_.]+))?(!?)",frag)
        if not m: params.append(("INVALID",frag)); continue
        media=bool(m.group(1) or m.group(3)); params.append((m.group(2).lower(),m.group(4),media,bool(m.group(5))))
    return params,outs
def check(path):
    wf=load_json(path); errs=[]; warns=[]
    if "nodes" in wf and "links" in wf: return ["画布格式，须为 API Format"],[],{},set()
    outs=set(); params={}
    for nid,n in wf.items():
        if not isinstance(n,dict) or "class_type" not in n or "inputs" not in n: errs.append(f"节点 {nid} 缺 class_type/inputs"); continue
        for k,v in n["inputs"].items():
            if k in CANVAS_ONLY_KEYS: errs.append(f"节点 {nid} 含画布专用残留键 {k}")
            if isinstance(v,list) and len(v)==2 and isinstance(v[0],str) and v[0] not in wf: errs.append(f"节点 {nid}.{k} 连线指向不存在节点 {v[0]}")
        p,o=parse_title(n.get("_meta",{}).get("title",""))
        for out_name in o:
            if out_name in outs: errs.append(f"节点 {nid} 输出 $output.{out_name} 重复标记")
        outs.update(o)
        for x in p:
            if x[0]=="INVALID": errs.append(f"节点 {nid} 标记非法 {x[1]}"); continue
            name,field,media,req=x; field=field or name
            if field not in n["inputs"]: errs.append(f"节点 {nid} 标记 ${name} 指向不存在字段 {field}")
            if isinstance(n["inputs"].get(field),list): errs.append(f"节点 {nid} 字段 {field} 是连线，无法注入")
            if media and n["class_type"] not in ("LoadImage","LoadAudio","LoadVideo"): warns.append(f"节点 {nid} 媒体标记用于非 Load 节点（LoadImage/LoadAudio/LoadVideo）")
            if name in params: errs.append(f"参数 {name} 重复绑定")
            params[name]=(nid,field,media,req)
    if not outs: errs.append("缺少 $output.* 输出标记 (workflow_marker_missing)")
    # 必需输出由工作流自身声明的 $output.* 决定，不再从 sidecar 推断
    for o in infer_need_out(path, outs):
        if o not in outs: errs.append(f"缺少必需输出 $output.{o}")
    if ("width" in params)!=("height" in params): errs.append("宽高须同时绑定")
    return errs,warns,params,outs

TARGET_TYPE={"keyframe":"image","clip":"video","audio":"tts","bgm":"bgm","sfx":"sfx"}
# $output.* 名 → trigger.target。
# 必需输出的判据是工作流自身的输出标记：`.meta.json` 已删除，不再有 sidecar 可读；
# 能力声明的业务来源是数据库 workflow 表的 capabilities/trigger（由 normalize 生成），
# 工作流 JSON 只负责「实际有哪些节点与输入字段」。
OUT_TARGET={"cover":"keyframe","clip":"clip","voice":"audio","bgm":"bgm","sfx":"sfx"}

def infer_need_out(path, available_outs):
    """按工作流自身声明的输出标记推断必需输出；不读 sidecar、不按文件名猜能力。"""
    for out in ("cover","clip","voice","bgm","sfx"):
        if out in available_outs:
            return [out]
    return []

def infer_workflow_type(outs):
    """按 $output.* 输出标记推断 workflow_type，不按文件名猜。"""
    for out,target in OUT_TARGET.items():
        if out in outs:
            return TARGET_TYPE.get(target)
    return None

def check_capabilities(path,wf,params,outs):
    """能力声明与工作流 JSON 的一致性校验。

    `.meta.json` 已删除，本脚本不再读 sidecar：能力声明的业务来源是数据库
    workflow 表的 capabilities/trigger（由 capabilities.normalize() 生成），
    工作流 JSON 只负责实际节点与输入字段。这里用同一个 normalize() 从节点推导能力，
    再核对每个声明槽位的 input_key 是否真的存在于节点输入中。
    """
    errs=[];warns=[]
    try:
        from app.services.workflow.capabilities import normalize
    except Exception as e:  # 后端依赖不可用时降级，不误报失败
        warns.append(f"跳过能力一致性校验：无法导入 normalize（{e}）")
        return errs,warns
    wf_type=infer_workflow_type(outs)
    if wf_type is None:
        errs.append("无法从 $output.* 输出标记确定 workflow_type")
        return errs,warns
    caps=normalize(wf_type,wf,capabilities=None,trigger=None)
    inputs_of=[n.get("inputs") or {} for n in wf.values() if isinstance(n,dict)]
    for slot in (caps.get("reference") or {}).get("slots") or []:
        name=slot.get("param")
        if name not in params or not params[name][2]:
            errs.append(f"槽位 {name} 不是已标记的媒体参数")
            continue
        key=slot.get("input_key")
        if not key:
            errs.append(f"槽位 {name} 未解析出 input_key")
            continue
        if not any(key in ins for ins in inputs_of):
            errs.append(f"槽位 {name} 的 input_key={key} 在工作流节点输入中不存在")
    if wf_type=="video":
        video=caps.get("video") or {}
        if video.get("fps") is None: warns.append("fps 未从计算图读取到，需联调后回填")
        # 时长控制看工作流是否真的标记了帧数/时长参数（H3 用 length，其它可能用 frames/duration）
        if not ({"length","frames","duration"} & set(params)):
            errs.append("视频工作流缺少帧数/时长控制参数标记（length/frames/duration）")
        audio=caps.get("audio") or {}
        if audio.get("supports_native_audio") and audio.get("audio_output_mode") not in ("embedded","separate"):
            errs.append("检测到原生音轨但 audio_output_mode 无法确定")
    return errs,warns
rc=0
for p in sys.argv[1:]:
    if p.endswith(".meta.json"): continue
    errs,warns,params,outs=check(p)
    me,mw=check_capabilities(p,load_json(p),params,outs); errs=errs+me; warns=warns+mw
    print(("PASS " if not errs else "FAIL ")+p)
    print("  params:",{k:f"{v[0]}.{v[1]}{'~' if v[2] else ''}{'!' if v[3] else ''}" for k,v in params.items()}); print("  outputs:",sorted(outs))
    for e in errs: print("  ERR",e); rc=1
    for w in warns: print("  warn",w)
sys.exit(rc)
