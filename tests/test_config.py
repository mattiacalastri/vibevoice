#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Contract tests for config.json — pill-owned settings (never touch live ~/.vibevoice)."""
from __future__ import annotations

import json

import config


def _redirect(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "CONFIG_TMP", tmp_path / "config.json.tmp")


def test_load_missing_file_returns_defaults(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    assert config.load() == config.DEFAULTS


def test_save_then_load_roundtrip(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    cfg = dict(config.DEFAULTS, lang="en", dock=False)
    config.save(cfg)
    assert config.load() == cfg
    assert not (tmp_path / "config.json.tmp").exists()  # atomic: no staging left


def test_corrupt_file_returns_defaults(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    (tmp_path / "config.json").write_text("{not json")
    assert config.load() == config.DEFAULTS


def test_existing_config_without_vp_gets_the_default(tmp_path, monkeypatch):
    """Adding a key must not require a migration: files written before it exist keep working."""
    _redirect(tmp_path, monkeypatch)
    (tmp_path / "config.json").write_text(json.dumps(
        {"lang": "en", "autosend": False, "autosend_return": False, "dock": False}))
    cfg = config.load()
    assert cfg["vp"] is True, "a pre-existing config must inherit the vp default"
    assert cfg["lang"] == "en", "the other values must survive untouched"


def test_vp_survives_a_roundtrip(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    config.save(dict(config.DEFAULTS, vp=False))
    assert config.load()["vp"] is False


def test_save_fills_omitted_keys_instead_of_raising(tmp_path, monkeypatch):
    """Regression: save() used to demand every key, so adding one armed a KeyError
    in any caller that built a partial dict (that is exactly what happened to
    settingsChanged_ when `vp` was introduced)."""
    _redirect(tmp_path, monkeypatch)
    config.save({"lang": "en"})                      # deliberately partial
    cfg = config.load()
    assert cfg["lang"] == "en"
    assert cfg["vp"] == config.DEFAULTS["vp"]
    assert cfg["dock"] == config.DEFAULTS["dock"]


def test_unknown_keys_dropped_known_kept(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    (tmp_path / "config.json").write_text(json.dumps({"lang": "en", "evil": 1}))
    cfg = config.load()
    assert cfg["lang"] == "en"
    assert "evil" not in cfg
