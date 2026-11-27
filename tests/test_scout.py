"""Offline tests: no network, no real key. Jev is replaced by a mock HTTP transport
behind the real TypeSafe SDK, so request validation and response parsing are exercised."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scout"))
import constants as C  # noqa: E402

import scout  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "github_items.json"
PROFILES = json.loads((ROOT / "data" / "success_profiles.json").read_text())
TODAY = dt.date(2026, 11, 17)


def fake_jev(category="Agent skills & plugins", fit=3, maturity=3, noul=0.9, injection=0.02, is_list=0.05, seen=None):
    import httpx2
    from typesafe_sdk import TypeSafeClient

    def handler(req):
        body = json.loads(req.content)
        if seen is not None:
            seen.append(body)
        ans = {}
        for k, q in body["questions"].items():
            if q["type"] == "noul":
                ans[k] = {"type": "noul", "noul": {"injection": injection, "is_list": is_list}.get(k, noul)}
            elif q["type"] == "choice":
                probs = {o: 0.0 for o in q["criteria"]}
                probs[category] = 0.95
                probs["other"] = 0.05
                ans[k] = {"type": "choice", "choice": category, "confidence": 0.94, "probabilities": probs}
            else:
                n, lv = len(q["criteria"]), {"fit": fit, "maturity": maturity}[k]
                ans[k] = {"type": "score", "score": float(lv), "confidence": 1.0,
                          "legend": {str(i): t for i, t in enumerate(q["criteria"])},
                          "probabilities": {str(i): float(i == lv) for i in range(n)}}
        return httpx2.Response(200, json={"model": C.JEV_MODEL, "answers": ans, "usage": {"input_tokens": 2000, "output_tokens": 40}})

    return TypeSafeClient(api_key="test-not-a-real-key", model=C.JEV_MODEL, transport=httpx2.MockTransport(handler))


def item(**kw):
    base = {"full_name": "acme/tool", "html_url": "https://github.com/acme/tool", "description": "A tool",
            "topics": ["agent-skills"], "language": "Python", "homepage": None, "stargazers_count": 5000,
            "forks_count": 400, "created_at": "2026-11-01T00:00:00Z", "pushed_at": "2026-11-16T00:00:00Z",
            "archived": False, "fork": False, "license": {"spdx_id": "MIT"}}
    base.update(kw)
    return base


def cand(it, jev=None):
    c = {"slug": it["full_name"], "metrics": scout.metrics(it, TODAY)}
    if jev:
        c["jev"] = jev
    return c


def jev_answers(cat="Agent skills & plugins", fit=3, maturity=3, noul=0.9, **over):
    nouls = {k: noul for k in C.COMMON_NOULS} | {k: noul for ch in C.TYPE_CHECKS.values() for k in ch}
    nouls.update({"injection": 0.02, "is_list": 0.05})
    nouls.update(over)
    return {"category": cat, "category_confidence": 0.9, "category_runner_up": "other", "fit": fit, "maturity": maturity, "nouls": nouls}


# ---------- metrics ----------
def test_metrics_basic():
    m = scout.metrics(item(), TODAY)
    assert m["age_days"] == 16 and m["stars_per_day"] == 312.5 and m["license"] == "MIT"
    assert m["days_since_push"] == 1 and m["padded_stars_flag"] is False


def test_padded_stars_flag():
    m = scout.metrics(item(stargazers_count=20000, forks_count=10), TODAY)
    assert m["padded_stars_flag"] is True


def test_noassertion_license_counts_as_none():
    assert scout.metrics(item(license={"spdx_id": "NOASSERTION"}), TODAY)["license"] is None


# ---------- questions ----------
def test_questions_validate_in_sdk():
    q = scout.build_questions()
    assert {"category", "fit", "maturity", "injection"} <= set(q)
    type_keys = [k for ch in C.TYPE_CHECKS.values() for k in ch]
    assert len(type_keys) == len(set(type_keys)), "type-check ids must be unique"
    assert set(C.TYPE_CHECKS) <= set(C.CATEGORIES), "every type-check category must be a category option"


# ---------- scoring gates ----------
def test_strong_repo_is_kept():
    c = cand(item(), jev_answers())
    scout.score(c, PROFILES)
    assert c["decision"] == "keep", c


def test_dry_run_never_keeps():
    c = cand(item())
    scout.score(c, PROFILES)
    assert c["decision"] in ("review", "skip")


def test_injection_blocks_keep():
    c = cand(item(), jev_answers(injection=0.9))
    scout.score(c, PROFILES)
    assert c["decision"] != "keep" and "injection text in README" in c["flags"]


def test_offtopic_blocks_keep():
    c = cand(item(), jev_answers(cat="other"))
    scout.score(c, PROFILES)
    assert c["decision"] != "keep"


def test_low_maturity_blocks_keep():
    c = cand(item(), jev_answers(maturity=1))
    scout.score(c, PROFILES)
    assert c["decision"] != "keep"


def test_list_outside_learning_blocks_keep():
    c = cand(item(), jev_answers(is_list=0.9))
    scout.score(c, PROFILES)
    assert c["decision"] != "keep" and "looks like a list" in c["flags"]


def test_token_saver_scored_on_checks_not_stars():
    slow = item(stargazers_count=60, forks_count=8)
    good = cand(slow, jev_answers(cat="Token & cost savers", noul=0.95))
    bad = cand(slow, jev_answers(cat="Token & cost savers", noul=0.05))
    scout.score(good, PROFILES)
    scout.score(bad, PROFILES)
    assert good["score"] - bad["score"] > 0.3


# ---------- end to end ----------
def test_run_end_to_end_with_mock_jev(tmp_path):
    db = tmp_path / "repos.json"
    db.write_text((ROOT / "data" / "repos.json").read_text())
    before = len(json.loads(db.read_text())["repos"])
    have = {r["slug"].lower() for r in json.loads(db.read_text())["repos"]}
    fresh = [it for it in json.loads(FIXTURE.read_text()) if it["full_name"].lower() not in have]
    seen = []
    summary = scout.run(["--db", str(db), "--out", str(tmp_path / "runs"), "--candidates-file", str(FIXTURE),
                         "--today", "2026-11-17"], client=fake_jev(seen=seen))
    assert 0 < len(fresh) < len(json.loads(FIXTURE.read_text())), "fixture should include one already-indexed repo"
    assert summary["candidates"] == len(fresh)  # dedupe against the index
    assert len(seen) == summary["candidates"] and seen[0]["model"] == C.JEV_MODEL
    assert "readme" in seen[0]["state"]["repo"]
    after = json.loads(db.read_text())["repos"]
    assert len(after) == before + summary["keep"]
    assert all(r["discovery_type"] == "Scout" for r in after[before:])
    assert (tmp_path / "runs" / "digest.md").read_text().startswith("# Repo scout, 2026-11-17")


def test_dry_run_writes_nothing_to_db(tmp_path):
    db = tmp_path / "repos.json"
    db.write_text((ROOT / "data" / "repos.json").read_text())
    original = db.read_text()
    summary = scout.run(["--db", str(db), "--out", str(tmp_path / "runs"), "--candidates-file", str(FIXTURE),
                         "--today", "2026-11-17", "--dry-run"])
    assert summary["keep"] == 0 and db.read_text() == original


def test_missing_key_exits_cleanly(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(scout, "load_dotenv", lambda *a, **k: None)
    with pytest.raises(SystemExit, match="TYPESAFE_API_KEY is not set"):
        scout.run(["--candidates-file", str(FIXTURE), "--out", str(tmp_path)])


def test_dotenv_does_not_override_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("SCOUT_TEST_VAR=from_file\n# comment\nSCOUT_TEST_OTHER='quoted'\n")
    monkeypatch.setenv("SCOUT_TEST_VAR", "from_env")
    monkeypatch.delenv("SCOUT_TEST_OTHER", raising=False)
    scout.load_dotenv(env)
    import os
    assert os.environ["SCOUT_TEST_VAR"] == "from_env" and os.environ["SCOUT_TEST_OTHER"] == "quoted"


def test_site_build_embeds_data_safely(tmp_path):
    import importlib.util
    import json as _json

    spec = importlib.util.spec_from_file_location("site_build", Path(__file__).resolve().parent.parent / "site" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    db = {"generated": "2026-11-20", "repos": [
        {"slug": "a/b", "url": "https://github.com/a/b", "category": "Security", "summary": "x </script><b>", "is_new": False,
         "times_featured": 2, "episodes": [{"video_id": "v1", "title": "t", "date": "2026-11-01", "url": "u"}]},
        {"slug": "c/d", "url": "https://github.com/c/d", "category": "Security", "summary": "new", "is_new": True},
    ]}
    (tmp_path / "repos.json").write_text(_json.dumps(db))
    out = mod.build(tmp_path / "repos.json", tmp_path / "_site")
    html = out.read_text()
    assert "__" + "DATA__" not in html and "</script><b>" not in html
    assert "Nov 20, 2026" in html and (tmp_path / "_site" / "repos.json").exists()
