import glob
import json
import os


def has_native_audio(definition: dict) -> bool:
    """检测计算图是否产出带音轨的视频。

    判据（可验证的结构事实，非名称推断）：
    - 存在 VAEDecodeAudio 节点（音频解码），且
    - 存在把音频接进视频的 CreateVideo/SaveVideo 节点
    """
    decode_audio = False
    mux_audio = False
    for node in definition.values():
        if not isinstance(node, dict):
            continue
        ct = (node.get("class_type") or "")
        inputs = node.get("inputs") or {}
        if "DecodeAudio" in ct:
            decode_audio = True
        if ct in ("CreateVideo", "SaveVideo", "VHS_VideoCombine"):
            for k in inputs:
                if "audio" in k.lower():
                    mux_audio = True
    return decode_audio and mux_audio


for p in sorted(glob.glob("data/workflows/wf_*.json")):
    if p.endswith(".meta.json"):
        continue
    d = json.load(open(p))
    name = os.path.basename(p)
    print(f"{name:40s} native_audio={has_native_audio(d)}")
