"""Verify the PictureBook LLM/image platform switch (SiliconFlow <-> NVIDIA).

The maker can switch platforms; each platform drives BOTH the chat LLMs and the
image generator:
  SiliconFlow -> DeepSeek-V4-Flash (chat) + Kwai-Kolors/Kolors (images)
  NVIDIA      -> deepseek-v4-pro   (chat) + Qwen-Image / Qwen-Image-Edit (images)
This checks the tool-side resolution: the active platform selects the right image
endpoint + model + key, and image-to-image uses the platform's edit model.
"""
import importlib.util
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import graph_codegen  # noqa: E402
import graph_model  # noqa: E402

g, _ = graph_model.load_mta(os.path.join(BASE, "graphs", "PictureBookAgent.mta"),
                            tempfile.mkdtemp())
out = graph_codegen.generate_from_graph(g, "verify_pb_platform", gui=False)
spec = importlib.util.spec_from_file_location("vpp", os.path.join(out, "agent.py"))
m = importlib.util.module_from_spec(spec)
sys.path.insert(0, out)
os.chdir(out)
spec.loader.exec_module(m)
os.chdir(BASE)

defs = m._PB.platform_defaults()
assert set(defs) == {"siliconflow", "nvidia"}, defs
# NVIDIA does CHAT (deepseek-v4-pro) but has no image API, so images use SiliconFlow's
# Qwen-Image / Qwen-Image-Edit (a HYBRID image provider).
assert defs["nvidia"]["chat_model"] == "meta/llama-3.1-70b-instruct", defs["nvidia"]["chat_model"]
assert "siliconflow" in defs["nvidia"]["image_url"], defs["nvidia"]["image_url"]
# mixed NVIDIA+SiliconFlow uses SiliconFlow's FREE Kolors for images (t2i + i2i)
assert defs["nvidia"]["image_model"] == "Kwai-Kolors/Kolors"
assert defs["nvidia"]["edit_model"] == "Kwai-Kolors/Kolors"
assert defs["siliconflow"]["image_model"] == "Kwai-Kolors/Kolors"
print("ok 1: profiles — SiliconFlow=Kolors/DeepSeek-Flash; NVIDIA=deepseek-v4-pro chat + "
      "SiliconFlow Kolors images (hybrid)")

# capture the outgoing image request per platform instead of hitting the network:
# gen_image posts through _PB._opener().open(...), so stub the opener.
captured = {}


class _Resp:
    def __init__(self, payload): self._p = payload
    def read(self): return json.dumps(self._p).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _FakeOpener:
    def open(self, req, timeout=0):
        captured["url"] = req.full_url
        captured["auth"] = dict(req.header_items()).get("Authorization")
        captured["body"] = json.loads(req.data.decode())
        return _Resp({"data": [{"b64_json": "aGk="}]})


_orig = m._PB._opener
m._PB._opener = staticmethod(lambda: _FakeOpener())
# each case: (config, expected image host, t2i model, i2i model, expected image key)
CASES = [
    # SiliconFlow: images on SiliconFlow with Kolors, using the SiliconFlow key
    ({"platform": "siliconflow", "platforms": {"siliconflow": {"api_key": "sk-sf"}}},
     "siliconflow", "Kwai-Kolors/Kolors", "Kwai-Kolors/Kolors", "Bearer sk-sf"),
    # NVIDIA: chat=NVIDIA, but images go to SiliconFlow Kolors with the IMAGE key
    ({"platform": "nvidia", "platforms": {
        "nvidia": {"api_key": "nvapi-x", "image_api_key": "sk-img"}}},
     "siliconflow", "Kwai-Kolors/Kolors", "Kwai-Kolors/Kolors", "Bearer sk-img"),
    # NVIDIA fallback: no image_api_key -> use the SiliconFlow platform's key
    ({"platform": "nvidia", "platforms": {
        "nvidia": {"api_key": "nvapi-x"}, "siliconflow": {"api_key": "sk-fallback"}}},
     "siliconflow", "Kwai-Kolors/Kolors", "Kwai-Kolors/Kolors", "Bearer sk-fallback"),
]
try:
    for cfg, host, model, edit, auth in CASES:
        m._PB.cfg = staticmethod(lambda c=cfg: c)
        tmp = tempfile.mkdtemp()
        m._PB.gen_image("a kitten", os.path.join(tmp, "t.png"))
        assert host in captured["url"], (cfg["platform"], captured["url"])
        assert captured["body"]["model"] == model, captured["body"]
        assert captured["auth"] == auth, (cfg["platform"], captured["auth"])
        ref = os.path.join(tmp, "ref.png"); open(ref, "wb").write(b"PNG")
        m._PB.gen_image("scene", os.path.join(tmp, "p.png"), source_path=ref)
        assert captured["body"]["model"] == edit, captured["body"]
        assert "image" in captured["body"], "i2i must send the reference image"
        print("ok: %-11s -> images@%s (t2i=%s, i2i=%s) key=%s"
              % (cfg["platform"], host, model, edit, auth))
finally:
    m._PB._opener = _orig

print("ok 2: image gen routes to the right (possibly hybrid) endpoint/model/key (t2i + i2i)")

import shutil  # noqa: E402
shutil.rmtree(out, ignore_errors=True)
print("ALL PICTUREBOOK-PLATFORM CHECKS PASSED")
