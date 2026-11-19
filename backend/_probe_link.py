import json

d = json.load(open("data/workflows/wf_video_h3_ref2va_std_5s.json"))
node6 = d["6"]
print("节点 6 class_type:", node6.get("class_type"))
for k, v in (node6.get("inputs") or {}).items():
    if any(t in k.lower() for t in ("ref_image", "ref_audio", "audio", "image")):
        print(f"  {k:28s} = {v}")

print()
print("节点 32 (LoadImage) inputs:", d.get("32", {}).get("inputs"))
