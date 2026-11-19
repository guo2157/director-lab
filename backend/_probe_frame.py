import json

from app.services.workflow.capabilities import normalize
from app.services.workflow.schema_builder import build_schema

d = json.load(open("data/workflows/wf_video_h3_ref2va_std_5s.json"))
caps = normalize("video", d)
print("frame_count_range:", caps["video"]["frame_count_range"])
print("fps              :", caps["video"]["fps"])
print("resolution       :", caps["video"]["resolution_constraints"])

schema = build_schema(d, capabilities=caps)
p = schema["params"].get("length")
print("length param     :", {k: p.get(k) for k in ("min", "max", "step", "default")} if p else None)
print("params count     :", len(schema["params"]))
print()
print("=== multi 工作流 slots ===")
d2 = json.load(open("data/workflows/wf_image_ref_qwen21_multi.json"))
c2 = normalize("image", d2)
slots = c2["reference"]["slots"]
print("slots count:", len(slots))
print("params:", [s["param"] for s in slots])
