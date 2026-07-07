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
# Graft the full trees from the build machine's site-packages, and purge the
# partial tiktoken from the zip so it cannot shadow the grafted copy.
BUNDLE_LIB="$APP/Contents/Resources/lib/python3.10"
zip -d "$APP/Contents/Resources/lib/python310.zip" 'tiktoken/*' >/dev/null 2>&1 || true
python3 - "$BUNDLE_LIB" <<'PYEOF'
import importlib.util, os, shutil, sys
bundle = sys.argv[1]
for pkg in ["mlx", "tiktoken", "tiktoken_ext", "tqdm", "filelock", "certifi", "llvmlite"]:
    dst = os.path.join(bundle, pkg)
    if os.path.exists(dst):
        continue
    spec = importlib.util.find_spec(pkg)
    if spec is None or not spec.submodule_search_locations:
        sys.exit(f"graft: cannot locate package {pkg} on the build machine")
    shutil.copytree(list(spec.submodule_search_locations)[0], dst)
    print(f"grafted {pkg}")
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
PYEOF
echo "Built: $APP"
