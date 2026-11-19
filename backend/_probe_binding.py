import json

from app.services.workflow.capabilities import normalize
from app.services.workflow.schema_builder import build_schema

d = json.load(open("data/workflows/wf_video_h3_ref2va_std_5s.json"))
caps = normalize("video", d)
schema = build_schema(d, capabilities=caps)

print("=== 媒体参数的结构 ===")
for name, p in list(schema["params"].items()):
    if p.get("type") in ("image", "audio", "video"):
        print(f"{name:20s} keys={sorted(p.keys())}")
        print(f"{'':20s} binding={p.get('binding')}")

print()
print("=== 归一化槽位里的 input_key ===")
for s in caps["reference"]["slots"][:4]:
    print(s)
