import json

d = json.load(open("data/workflows/wf_video_h3_ref2va_std_5s.json"))
print("=== 含 length/frame/fps/width/height 的节点输入 ===")
for nid, node in d.items():
    if not isinstance(node, dict):
        continue
    inputs = node.get("inputs") or {}
    hits = {k: v for k, v in inputs.items()
            if any(t in str(k).lower() for t in ("length", "frame", "fps", "width", "height"))}
    if hits:
        print(f"  [{nid}] {node.get('class_type')}: {hits}")
