# -*- coding: utf-8 -*-
"""逐镜头生成中文解说，并检查每条时长是否落在镜头槽位内。"""
import os, subprocess, wave, json
import numpy as np

SR = 48000
PY = r"C:\Users\guoheng\AppData\Local\Programs\Python\Python312\python.exe"
VOICE = "zh-CN-YunyangNeural"
RATE = "+5%"

# (id, 起始秒, 镜头结束秒, 文案)
LINES = [
    ("v01", 5.55, 10.0, "Director-Lab，自托管的漫剧生产台。"),
    ("v02", 10.15, 16.6, "难的不是生成一帧，是让二十六帧接得上。"),
    ("v03", 16.85, 22.6, "剧本不再是提示词，是有场次、有人物的本子。"),
    ("v04", 22.85, 29.8, "一次解析拆到分镜级：两个角色，四件道具，二十六个镜头。"),
    ("v05", 30.00, 36.6, "先定妆，再开工。同一张脸，贯穿二十六个镜头。"),
    ("v06", 36.80, 43.4, "二十六个镜头，每一个都能单独重做。"),
    ("v07", 43.60, 51.2, "上一镜的尾帧，就是下一镜的首帧。这是连贯的唯一办法。"),
    ("v08", 51.35, 58.4, "字幕、配音、音轨一次对齐，直接导出成片。"),
    ("v09", 58.60, 64.8, "跑得久也不会烂尾。今天成功一百一十六，零堆积。"),
    ("v10", 64.95, 70.6, "两百二十七个接口，二十八个工具，六百二十四个测试。"),
    ("v11", 70.80, 75.0, "把导演，从重复劳动里解放出来。"),
]

os.makedirs("audio/vo", exist_ok=True)
rows = []
for vid, start, end, text in LINES:
    mp3 = f"audio/vo/{vid}.mp3"
    wav = f"audio/vo/{vid}.wav"
    if not (os.path.exists(wav) and os.path.getmtime(wav) > os.path.getmtime(__file__)):
        subprocess.run(
            [PY, "-m", "edge_tts", "--voice", VOICE, f"--rate={RATE}",
             "--text", text, "--write-media", mp3],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", mp3, "-ar", str(SR), "-ac", "2", wav],
            check=True,
        )
    with wave.open(wav) as w:
        dur = w.getnframes() / w.getframerate()
    slot = end - start
    rows.append(dict(id=vid, start=start, dur=round(dur, 2), slot=round(slot, 2),
                     end=round(start + dur, 2), text=text,
                     ok=start + dur <= end + 0.15))

print(f"{'id':<4}{'start':>7}{'dur':>7}{'slot':>7}{'end':>7}  fit  text")
for r in rows:
    print(f"{r['id']:<4}{r['start']:>7}{r['dur']:>7}{r['slot']:>7}{r['end']:>7}  "
          f"{'OK ' if r['ok'] else 'OVER'} {r['text']}")
print("\noverflow:", [r['id'] for r in rows if not r['ok']] or "none")
json.dump(rows, open("audio/vo/lines.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
