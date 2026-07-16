#!/bin/bash
# SPDX-License-Identifier: MIT
# Self-contained VibeVoice.app (py2app). Run from repo root: bash packaging/build_release.sh
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pip show py2app >/dev/null || python3 -m pip install py2app
rm -rf packaging/build packaging/dist
python3 packaging/setup_py2app.py py2app --dist-dir packaging/dist --bdist-base packaging/build
APP="packaging/dist/VibeVoice.app"

echo "── graft: packages modulegraph cannot see ──"
# modulegraph's AST scan misses these at build time:
#   mlx, tiktoken_ext — PEP 420 namespace packages (no __init__.py)
#   tiktoken          — bundled PARTIALLY into the zip (py files reachable from
#                       annotations, but not core/__init__/the rust .so)
#   tqdm/filelock/certifi — behind huggingface_hub's lazy __getattr__ imports
#   llvmlite          — loaded by numba through ctypes/dylib indirection
#   onnxruntime       — DELIBERATELY excluded from the scan in setup_py2app.py
#                       (modulegraph crashes on the host's ML cluster); the F2
#                       Silero decider lazy-imports it, so graft the real tree
# Graft the full trees from the build machine's site-packages, and purge the
# partial tiktoken AND mlx from the zip so they cannot shadow the grafted
# copies. mlx is the worst case: it has no __init__.py (namespace package), so
# the zip's synthesized REGULAR mlx package (stub __init__.pyc + core.pyc
# extension stub, missing _reprlib_fix) beats the grafted tree at ANY sys.path
# position — regular packages always win over namespace portions.
BUNDLE_LIB="$APP/Contents/Resources/lib/python3.10"
zip -d "$APP/Contents/Resources/lib/python310.zip" 'tiktoken/*' 'mlx/*' >/dev/null 2>&1 || true
python3 - "$BUNDLE_LIB" <<'PYEOF'
import importlib.util, os, shutil, sys
bundle = sys.argv[1]
for pkg in ["mlx", "tiktoken", "tiktoken_ext", "tqdm", "filelock", "certifi", "llvmlite",
            "onnxruntime"]:
    dst = os.path.join(bundle, pkg)
    if os.path.exists(dst):
        continue
    spec = importlib.util.find_spec(pkg)
    if spec is None or not spec.submodule_search_locations:
        sys.exit(f"graft: cannot locate package {pkg} on the build machine")
    shutil.copytree(list(spec.submodule_search_locations)[0], dst)
    print(f"grafted {pkg}")
PYEOF

echo "── graft: Silero VAD model (op15 16k variant when equivalent) ──"
# engine._resolve_silero_model probes silero_vad/data/silero_vad.onnx next to a
# CONCRETE silero_vad/__init__.py (find_spec on a namespace package has
# origin=None and the cascade fails). Ship ONLY the model file: the real
# silero_vad __init__ imports torch, and its data dir carries ~7 MB of spare
# variants (.jit, half, op18) the engine never reads.
# GATE 1 decision: prefer the 1.3 MB 16k-only op15 variant, but only after
# proving it equivalent to the full 2.3 MB model ON THIS BUILD MACHINE —
# identical state-threaded probabilities (tol 1e-4) on a synthetic
# silence→speech→silence sweep AND identical SILERO_ONSET/SILERO_OFFSET
# hysteresis gate decisions. Any doubt or error → ship the full model.
python3 - "$BUNDLE_LIB" <<'PYEOF'
import importlib.util, os, shutil, sys
import numpy as np
bundle = sys.argv[1]
spec = importlib.util.find_spec("silero_vad")
if spec is None or not spec.origin:
    sys.exit("graft: cannot locate package silero_vad on the build machine")
data = os.path.join(os.path.dirname(spec.origin), "data")
full = os.path.join(data, "silero_vad.onnx")
op15 = os.path.join(data, "silero_vad_16k_op15.onnx")
if not os.path.exists(full):
    sys.exit(f"graft: {full} does not exist")

def probs(model_path):
    """State-threaded per-frame speech probabilities, engine-identical feeding
    (64-sample context + 512-sample frame, state (2,1,128), sr int64)."""
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.inter_op_num_threads = opts.intra_op_num_threads = 1
    opts.log_severity_level = 3
    sess = ort.InferenceSession(model_path, sess_options=opts,
                                providers=["CPUExecutionProvider"])
    state = np.zeros((2, 1, 128), dtype=np.float32)
    ctx = np.zeros(64, dtype=np.float32)
    sr = np.array(16000, dtype=np.int64)
    rng = np.random.default_rng(9465)
    t = np.arange(512 * 40) / 16000.0
    speech = (0.3 * np.sin(2 * np.pi * 160 * t) * (1 + 0.5 * np.sin(2 * np.pi * 4 * t))
              + 0.05 * rng.standard_normal(t.size))
    quiet = 0.005 * rng.standard_normal(512 * 20)
    sig = np.concatenate([quiet, speech, quiet]).astype(np.float32)
    out = []
    for off in range(0, sig.size - 511, 512):
        x = np.concatenate([ctx, sig[off:off + 512]]).reshape(1, -1)
        o = sess.run(None, {"input": x, "state": state, "sr": sr})
        state = np.asarray(o[1], dtype=np.float32)
        ctx = x[0, -64:]
        out.append(float(np.asarray(o[0]).reshape(-1)[0]))
    return np.array(out)

chosen = full
if os.path.exists(op15):
    try:
        a, b = probs(full), probs(op15)
        same_probs = float(np.abs(a - b).max()) <= 1e-4
        same_gates = bool(((a >= 0.5) == (b >= 0.5)).all()       # SILERO_ONSET
                          and ((a <= 0.35) == (b <= 0.35)).all())  # SILERO_OFFSET
        if same_probs and same_gates:
            chosen = op15
        else:
            print(f"silero: op15 NOT equivalent (max prob diff "
                  f"{float(np.abs(a - b).max()):.6f}) — shipping the full model")
    except Exception as err:
        print(f"silero: equivalence check failed ({err}) — shipping the full model")
dst_dir = os.path.join(bundle, "silero_vad", "data")
os.makedirs(dst_dir, exist_ok=True)
open(os.path.join(bundle, "silero_vad", "__init__.py"), "w").close()
shutil.copy2(chosen, os.path.join(dst_dir, "silero_vad.onnx"))
print(f"grafted silero model: {os.path.basename(chosen)} "
      f"({os.path.getsize(chosen)} bytes) -> silero_vad/data/silero_vad.onnx")
PYEOF

echo "── smoke: bundle is self-contained ──"
plutil -lint "$APP/Contents/Info.plist"
test -x "$APP/Contents/MacOS/VibeVoice"
test -d "$APP/Contents/Resources/lib" && echo "embedded python: OK"
# Import the ML stack from INSIDE the bundle and assert nothing leaks from the
# host's site-packages (python3 -I still sees the global site dir, so each
# module's __file__ must be checked explicitly).
python3 -I - "$BUNDLE_LIB" <<'PYEOF'
import os, sys
bundle = os.path.abspath(sys.argv[1])
sys.path.insert(0, bundle)
import mlx.core as mc
import mlx_whisper, numpy, sounddevice, tiktoken
for m in (mlx_whisper, tiktoken, numpy):
    assert m.__file__.startswith(bundle), f"{m.__name__} leaked: {m.__file__}"
assert mc.__file__.startswith(bundle), f"mlx.core leaked: {mc.__file__}"
enc = tiktoken.get_encoding("gpt2")
assert enc.decode(enc.encode("vibevoice")) == "vibevoice"
print("import smoke: OK —", mc.default_device())

# F2: onnxruntime grafted + the Silero model answers from INSIDE the bundle.
import onnxruntime
assert onnxruntime.__file__.startswith(bundle), f"onnxruntime leaked: {onnxruntime.__file__}"
model = os.path.join(bundle, "silero_vad", "data", "silero_vad.onnx")
assert os.path.isfile(model), f"silero model not grafted: {model}"
sess = onnxruntime.InferenceSession(model, providers=["CPUExecutionProvider"])
out = sess.run(None, {"input": numpy.zeros((1, 576), dtype=numpy.float32),
                      "state": numpy.zeros((2, 1, 128), dtype=numpy.float32),
                      "sr": numpy.array(16000, dtype=numpy.int64)})
prob = float(numpy.asarray(out[0]).reshape(-1)[0])
assert 0.0 <= prob <= 1.0, f"silero inference out of range: {prob}"
print("onnxruntime smoke: OK —", onnxruntime.__version__)
print(f"silero smoke: OK — {os.path.getsize(model)} bytes, p(silence)={prob:.3f}")
PYEOF
# F1: AVFoundation importable from the bundle. py2app puts pyobjc pure modules
# in python310.zip and their extensions in lib-dynload — a SEPARATE -I
# invocation, because putting the zip on the ML smoke's path would shadow the
# host stdlib with the bundle's ssl.pyc, whose _ssl.so resolves
# @executable_path against the HOST interpreter and dlopen-fails (the real app
# resolves it against Contents/MacOS and is fine).
python3 -I - "$BUNDLE_LIB" <<'PYEOF'
import os, sys
bundle = os.path.abspath(sys.argv[1])
res = os.path.dirname(os.path.dirname(bundle))          # .../Contents/Resources
sys.path[:0] = [os.path.join(os.path.dirname(bundle), "python310.zip"),
                os.path.join(bundle, "lib-dynload")]
import AVFoundation
assert AVFoundation.__file__.startswith(res), f"AVFoundation leaked: {AVFoundation.__file__}"
print("AVFoundation smoke: OK")
PYEOF
echo "Built: $APP"
