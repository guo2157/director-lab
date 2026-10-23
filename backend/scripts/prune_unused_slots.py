#!/usr/bin/env python3
"""提交 ComfyUI 前剔除未注入的可选参考槽位。
用法(库): prune(workflow_dict, provided_param_names) -> workflow_dict
规则: 标题含 $name.~field 的 LoadImage/LoadAudio，若 name 不在 provided 中，则删除该节点，并删除所有指向它的输入键。
视频工作流剔除后，H3 的 'Image N' 按剩余连线顺序重新编号，提示词须按剩余槽位生成。"""
import json, re, sys, copy
PASS_THROUGH_KEYS=("image","audio","video")
def prune(wf, provided):
    """剔除未注入的可选媒体槽位，并级联清理：
    1) 删除未注入的 Load* 节点；2) 指向已删节点的输入键一律删除；
    3) 若被删的输入键属于透传主输入(image/audio/video)，该节点本身也无意义(如 ImageScale)，一并删除并继续级联。"""
    wf=copy.deepcopy(wf); drop=set()
    for nid,n in wf.items():
        titles=n.get("_meta",{}).get("title","")
        ms=list(re.finditer(r"\$(?:~([A-Za-z0-9_]+)|([A-Za-z0-9_]+)\.~)",titles))
        if n["class_type"] in ("LoadImage","LoadAudio","LoadVideo") and ms and all((m.group(1) or m.group(2)).lower() not in provided for m in ms): drop.add(nid)
    changed=True
    while changed:
        changed=False
        for nid,n in wf.items():
            if nid in drop: continue
            for k,v in list(n["inputs"].items()):
                if isinstance(v,list) and v and v[0] in drop:
                    if k in PASS_THROUGH_KEYS: drop.add(nid); changed=True; break
    for n in wf.values():
        for k in [k for k,v in n["inputs"].items() if isinstance(v,list) and v and v[0] in drop]: del n["inputs"][k]
    for d in drop: del wf[d]
    return wf
if __name__=="__main__":
    wf=json.load(open(sys.argv[1])); print(json.dumps(prune(wf,set(sys.argv[2:])),ensure_ascii=False,indent=2))
