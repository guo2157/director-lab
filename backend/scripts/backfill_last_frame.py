import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal
from app.core.config import get_settings
from app.models import Asset, Shot
from app.services.shot_chain import store_shot_last_frame

SHOT = 'sht_cc122825183d'
CLIP_ASSET = 'ast_f1bf1f572b09'

db = SessionLocal()
try:
    shot = db.get(Shot, SHOT)
    if shot is None:
        raise SystemExit('shot not found')
    print('shot before:', shot.id, 'last_frame_asset_id =', shot.last_frame_asset_id)
    if shot.last_frame_asset_id:
        old = db.get(Asset, shot.last_frame_asset_id)
        print('already bound:', old.id if old else None, old.validity if old else None)
    clip = db.get(Asset, CLIP_ASSET)
    if clip is None:
        raise SystemExit('clip asset not found')
    print('clip asset:', clip.id, clip.asset_type, clip.validity, clip.file_path, os.path.exists(clip.file_path))
    result = store_shot_last_frame(db, shot, clip, get_settings().media_dir)
    print('store result:', result)
    if result.get('ok'):
        db.commit()
        db.refresh(shot)
        print('shot after:', shot.id, 'last_frame_asset_id =', shot.last_frame_asset_id)
    else:
        db.rollback()
        print('FAILED, rolled back')
finally:
    db.close()
