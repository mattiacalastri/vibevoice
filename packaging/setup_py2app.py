#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""py2app build: python3 packaging/setup_py2app.py py2app (run from repo root).

Fallback if py2app can't cope (document WHY in the commit):
  pyinstaller --windowed --name VibeVoice --icon assets/icon/VibeVoice.icns vibevoice.py
  then: check libportaudio via `otool -L` and graft with install_name_tool.
"""
import sys

from setuptools import setup

# modulegraph walks the AST of every scanned module recursively (no explicit
# stack) — numpy's __init__ has deeply nested conditional imports that blow
# past Python's default recursion limit (1000) during the py2app scan itself
# (not at runtime). Bump it for the duration of the build only.
sys.setrecursionlimit(10000)

APP = ["vibevoice.py"]
DATA_FILES = ["engine.py", "autosend.py", "config.py", "requirements.txt"]
OPTIONS = {
    "iconfile": "assets/icon/VibeVoice.icns",
    # mlx_whisper is deliberately NOT in "packages": that option forces py2app
    # to recursively bundle the ENTIRE package tree, which for mlx_whisper
    # drags in transformers' optional torch/jax/onnxruntime/numba backends
    # (all installed system-wide here) and blows up on Windows-only PyInstaller
    # hook stubs (`No module named PyInstaller.hooks.hook-PyQt5.QtX11Extras`).
    # engine.py already lazy-imports mlx_whisper inside a guarded try/except
    # (_ensure_mlx_whisper), so "includes" (normal reachability-based
    # discovery, following the actual import graph) is enough to ship it
    # without forcing in its unused optional heavyweight dependencies.
    "packages": ["numpy", "sounddevice"],
    "includes": ["objc", "AppKit", "Foundation", "Quartz", "config", "mlx_whisper"],
    # This Mac's system site-packages is polluted with heavyweight ML backends
    # (torch, jax, onnxruntime, PyInstaller, ...) that mlx_whisper's transitive
    # graph can reach but never needs at runtime. modulegraph crashes scanning
    # PyInstaller's dashed hook filenames (`hook-PySide2.QtWebEngine` is not an
    # importable module name), so cut the whole cluster out of the scan.
    "excludes": [
        "PyInstaller",
        "torch",
        "torchvision",
        "torchaudio",
        "torchgen",
        "jax",
        "jaxlib",
        "onnxruntime",
        "matplotlib",
        "sympy",
        "pandas",
        "PIL",
        "black",
        "pytest",
        "test",
    ],
    "plist": {
        "CFBundleName": "VibeVoice",
        "CFBundleDisplayName": "VibeVoice",
        "CFBundleIdentifier": "com.vibevoice.app",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "LSMinimumSystemVersion": "12.0",
        "LSUIElement": False,  # Dock ON by default; runtime policy follows config.json
        "NSMicrophoneUsageDescription": "VibeVoice transcribes your voice into text, locally on your Mac.",
        "NSAppleEventsUsageDescription": "VibeVoice uses AppleScript to detect the frontmost app and press Return after dictation.",
    },
}

setup(app=APP, data_files=DATA_FILES, options={"py2app": OPTIONS}, setup_requires=["py2app"])
