from app.core.database import SessionLocal
from app.models import Workflow
from app.services import regeneration as regen

db = SessionLocal()
try:
    for name in ("wf_video_h3_ref2va_std_5s", "wf_image_ref_qwen21_multi"):
        wf = db.query(Workflow).filter_by(name=name).first()
        slots = regen.normalize_reference_slots(wf.capabilities if wf else None)
        print(f"{name}: 归一化槽位数 = {len(slots)}")
        for s in slots[:3]:
            print("   ", s)
        print()
finally:
    db.close()
