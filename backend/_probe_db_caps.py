import json

from app.core.database import SessionLocal
from app.models import Workflow

db = SessionLocal()
try:
    for wf in db.query(Workflow).all():
        caps = wf.capabilities if isinstance(wf.capabilities, dict) else {}
        if not caps:
            try:
                caps = json.loads(wf.capabilities or "{}")
            except Exception:
                caps = {}
        audio = caps.get("audio") or {}
        print(f"{wf.name:38s} type={wf.workflow_type:6s} "
              f"native_audio={audio.get('supports_native_audio')} "
              f"audio_out_mode={audio.get('audio_output_mode')} "
              f"max_img_ref={ (caps.get('reference') or {}).get('max_image_references') }")
finally:
    db.close()
