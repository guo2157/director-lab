import json
import collections

d = json.load(open("data/workflows/wf_video_h3_ref2va_std_5s.json"))
print("=== H3 节点 class_type 汇总 ===")
ct = collections.Counter(n.get("class_type") for n in d.values() if isinstance(n, dict))
for k, v in ct.items():
    print(f"  {v}x {k}")

print()
print("=== 含 audio 字样的节点/输入 ===")
for nid, node in d.items():
    if not isinstance(node, dict):
        continue
    c = (node.get("class_type") or "").lower()
    inputs = node.get("inputs") or {}
    keys = [k for k in inputs if "audio" in k.lower()]
    if "audio" in c or keys:
        print(f"  [{nid}] {node.get('class_type')}  inputs_with_audio={keys}")
