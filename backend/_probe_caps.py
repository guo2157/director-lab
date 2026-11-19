import json

from app.services.workflow.capabilities import normalize

d = json.load(open("data/workflows/wf_video_h3_ref2va_std_5s.json"))
caps = normalize("video", d, capabilities=None, trigger=None)
print("=== H3 ref2va std_5s（无 meta）===")
print("audio_input          :", caps["modalities"]["audio_input"])
print("max_audio_inputs     :", caps["audio"]["max_audio_inputs"])
print("supports_native_audio:", caps["audio"]["supports_native_audio"])
print("max_image_references :", caps["reference"]["max_image_references"])
print("generation_modes     :", caps["generation_modes"])
print("audio_slot_semantics :",
      {s["semantic"] for s in caps["reference"]["slots"] if s["media_type"] == "audio"})

print()
d2 = json.load(open("data/workflows/wf_image_t2i_qwen21_turbo.json"))
c2 = normalize("image", d2, capabilities=None, trigger=None)
print("=== T2I qwen21 turbo（无 meta）===")
print("audio_input :", c2["modalities"]["audio_input"])
print("image_output:", c2["modalities"]["image_output"])
print("modes       :", c2["generation_modes"])
