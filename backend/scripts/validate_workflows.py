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
def check(path,need_out):
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
    for o in need_out:
        if o not in outs: errs.append(f"缺少必需输出 $output.{o}")
    if ("width" in params)!=("height" in params): errs.append("宽高须同时绑定")
    return errs,warns,params,outs

TARGET_TYPE={"keyframe":"image","clip":"video","audio":"tts","bgm":"bgm","sfx":"sfx"}
# trigger.target → 必需输出名；meta 缺失或 target 非法时回退到文件名推断
TARGET_OUT={"keyframe":"cover","clip":"clip","audio":"voice","bgm":"bgm","sfx":"sfx"}

def meta_path_of(path):
    return os.path.splitext(path)[0]+".meta.json"

def infer_need_out(path):
    """优先依据 sidecar meta 的 trigger.target 推断必需输出；失败时回退文件名推断。"""
    mp=meta_path_of(path)
    if os.path.exists(mp):
        try:
            t=load_json(mp).get("trigger",{}).get("target")
            if t in TARGET_OUT: return [TARGET_OUT[t]]
        except Exception:
            pass
    base=os.path.basename(path)
    return ["clip"] if "video" in base else ["voice"] if "tts" in base else ["bgm"] if "bgm" in base else ["sfx"] if "sfx" in base else ["cover"]

def check_meta(path,wf,params,outs):
    mp=meta_path_of(path); errs=[];warns=[]
    if not os.path.exists(mp): return ["缺少 sidecar "+os.path.basename(mp)],[]
    m=load_json(mp)
    legacy = m.get("schema_version") is None
    if legacy:
        warns.append("meta 为旧格式（无 schema_version），建议升级到 schema_version 1")
    else:
        if m.get("schema_version")!=1: errs.append("schema_version 须为 1")
        if m.get("workflow_file")!=os.path.basename(path): errs.append("workflow_file 与文件名不一致")
    t=m.get("trigger",{}); c=m.get("capabilities",{})
    if t.get("target") not in TARGET_TYPE: errs.append(f"trigger.target 非法 {t.get('target')}")
    elif c.get("workflow_type")!=TARGET_TYPE[t["target"]]: errs.append("workflow_type 与 target 映射不符")
    if c.get("target")!=t.get("target"): errs.append("capabilities.target 与 trigger.target 不一致")
    for p in t.get("required_params",[]):
        if p not in params: errs.append(f"required_params 含未标记参数 {p}")
    o=c.get("output","")
    if not o.startswith("$output.") or o[8:] not in outs: errs.append(f"capabilities.output {o} 在工作流中不存在")
    for s in c.get("reference_slots",[]):
        name = s if isinstance(s,str) else s.get("param")
        if name not in params or not params[name][2]: errs.append(f"reference_slots 参数 {name} 不是已标记的媒体参数")
    for k in ("ratio_patch_key","resolution_patch_key"):
        v=c.get(k)
        if v and not any(v in n["inputs"] for n in wf.values()): errs.append(f"{k}={v} 在工作流输入中不存在")
    fp=c.get("frame_count_param")
    if fp and fp not in params: errs.append(f"frame_count_param {fp} 未标记")
    if c.get("workflow_type")=="video" and not (c.get("duration_param") or fp): errs.append("video 须声明 duration_param 或 frame_count_param 之一")
    if fp and (c.get("fps") is None or c.get("frame_count_step") is None): errs.append("frame_count_param 存在时 fps 与 frame_count_step 必填")
    dp=c.get("duration_param")
    if dp and dp not in params: errs.append(f"duration_param {dp} 未标记")
    if c.get("workflow_type")=="video" and c.get("audio") and c.get("audio_output_mode") not in ("embedded","separate"): errs.append("audio=true 时须声明 audio_output_mode")
    if c.get("workflow_type")=="video" and c.get("fps") is None: warns.append("fps 未声明(null)，需联调后回填")
    return errs,warns
rc=0
for p in sys.argv[1:]:
    if p.endswith(".meta.json"): continue
    need=infer_need_out(p)
    errs,warns,params,outs=check(p,need)
    me,mw=check_meta(p,load_json(p),params,outs); errs=errs+me; warns=warns+mw
    print(("PASS " if not errs else "FAIL ")+p)
    print("  params:",{k:f"{v[0]}.{v[1]}{'~' if v[2] else ''}{'!' if v[3] else ''}" for k,v in params.items()}); print("  outputs:",sorted(outs))
    for e in errs: print("  ERR",e); rc=1
    for w in warns: print("  warn",w)
sys.exit(rc)
