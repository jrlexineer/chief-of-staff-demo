"""test_composition.py — importing the app does nothing; constructing it does everything.

Not an audit finding. Two open questions in PROJECT_STATUS.md ended here:

  - `main.py` called `load_policy()` and `store.init()` at import time, so importing
    the module read the filesystem and created a database, and a broken policy file
    crashed at import rather than at startup — where the failure is legible and where
    a supervisor can act on it.
  - Policy was a per-call parameter that any caller could hand-construct. It still is,
    at the functions that use it: that is what keeps the gate a pure function of
    (extraction, ground truth, policy). What changes is that exactly one place in the
    process CONSTRUCTS one, so a second policy in flight is now a deliberate act
    rather than an accident of a default argument.

    python -m pytest tests/test_composition.py
"""
import pathlib
import subprocess
import sys
import textwrap

import pytest
import yaml
from fastapi import FastAPI

from app.main import create_app
from app.policy_loader import POLICY_PATH


def test_importing_main_has_no_side_effects(tmp_path):
    """Run in a fresh interpreter: this test file has already imported the module, and
    an import that only misbehaves the first time still misbehaves."""
    db = tmp_path / "should-not-exist.sqlite"
    script = textwrap.dedent(f"""
        import pathlib, sys
        sys.path.insert(0, {str(pathlib.Path.cwd())!r})
        from app import store
        store.DB = pathlib.Path({str(db)!r})
        import app.main
        print("DIRTY" if store.DB.exists() else "CLEAN")
        print("MODULE_LEVEL_APP" if hasattr(app.main, "app") else "NO_MODULE_LEVEL_APP")
    """)
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=pathlib.Path.cwd()
    )
    assert out.returncode == 0, out.stderr
    assert "CLEAN" in out.stdout
    assert "NO_MODULE_LEVEL_APP" in out.stdout
    assert not db.exists()


def test_create_app_builds_a_working_app(tmp_path):
    app = create_app(db_path=tmp_path / "audit.sqlite")
    assert isinstance(app, FastAPI)
    assert app.state.policy.policy_hash
    paths = {r.path for r in app.routes}
    assert {"/health", "/queue", "/queue/{audit_id}/resolve", "/stats"} <= paths
    assert (tmp_path / "audit.sqlite").exists()          # the db is created HERE


def test_a_missing_policy_file_fails_at_startup(tmp_path):
    with pytest.raises(FileNotFoundError):
        create_app(policy_path=tmp_path / "nope.yaml", db_path=tmp_path / "a.sqlite")


def test_a_broken_policy_file_fails_at_startup(tmp_path):
    """The skew check from L3.2, reached at construction instead of at import."""
    raw = yaml.safe_load(POLICY_PATH.read_bytes())
    raw["documents"]["known_doc_types"] = raw["documents"]["known_doc_types"] + ["lease"]
    bad = tmp_path / "policy.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="lease"):
        create_app(policy_path=bad, db_path=tmp_path / "a.sqlite")


def test_the_policy_reaches_the_endpoint_from_app_state(tmp_path):
    app = create_app(db_path=tmp_path / "audit.sqlite")

    class _Request:                     # the one attribute the endpoint touches
        def __init__(self, app):
            self.app = app

    from app import main
    body = main.health(_Request(app))
    assert body["ok"] is True
    assert body["policy_hash"] == app.state.policy.policy_hash
