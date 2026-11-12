import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else 'data/workflows/wf_video_h3_ref2va_std_5s.json'
d = json.load(open(path, encoding='utf-8'))
print('NODE_COUNT', len(d))
for k, v in d.items():
    ct = v.get('class_type')
    ins = v.get('inputs') or {}
    brief = {}
    for ik, iv in ins.items():
        if isinstance(iv, list):
            brief[ik] = 'LINK:%s' % (iv[0] if iv else '?')
        else:
            s = str(iv)
            brief[ik] = s[:120]
    print('---', k, ct)
    print(json.dumps(brief, ensure_ascii=False, indent=1)[:900])
