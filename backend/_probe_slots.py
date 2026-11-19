from app.core.database import SessionLocal
from app.models import Workflow

db = SessionLocal()
try:
    wf = db.query(Workflow).filter_by(name="wf_video_h3_ref2va_std_5s").first()
    caps = wf.capabilities or {}
    print("caps 顶层 keys:", sorted(caps.keys()))
    print()
    print("有 reference_slots（旧扁平键）:", "reference_slots" in caps)
    print("有 reference.slots（新结构）  :", "slots" in (caps.get("reference") or {}))
    print()
    new_slots = (caps.get("reference") or {}).get("slots") or []
    print("新结构槽位数:", len(new_slots))
    for s in new_slots:
        print("  ", s.get("param"), "| input_key:", s.get("input_key"),
              "| semantic:", s.get("semantic"))
finally:
    db.close()
