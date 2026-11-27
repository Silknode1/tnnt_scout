#!/usr/bin/env python3
"""TNNT repo scout: find new GitHub repos, judge them by tool type with Jev, rank in code.

Pipeline (code owns the workflow; Jev only answers narrow typed questions):
  1. collect   GitHub search: young fast-growing repos + topic sweeps (constants.py)
  2. dedupe    drop anything already in repos.json (featured or previously scouted)
  3. metrics   code: stars/day, days since push, license, fork ratio, padded-star flag
  4. judge     one Jev request per repo: category Choice, fit + maturity Scores,
               common Nouls, and every type-specific Noul (premise written in)
  5. score     code: weighted composite using the chosen category's weights and checks
  6. write     runs/<date>.json (all candidates), runs/digest.md, and keepers appended
               to repos.json with is_new=true, discovery_type="Scout", found_on=<date>

Usage (from the repo root):
  python scout/scout.py                 # live run: needs TYPESAFE_API_KEY
  python scout/scout.py --dry-run       # no Jev, no key needed; nothing is auto-kept
Env (read from the environment, or from a local .env file that git ignores):
  TYPESAFE_API_KEY   read by the TypeSafe SDK; never passed or printed by this code
  GITHUB_TOKEN       optional locally, provided automatically in GitHub Actions
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import constants as C  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GH = "https://api.github.com"


# ---------------- environment ----------------
def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Load KEY=VALUE lines from a local .env without overriding real env vars. Values are never printed."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


# ---------------- GitHub (code) ----------------
def gh_get(path: str, params: dict | None = None) -> dict:
    url = GH + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "tnnt-scout"})
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < 2:  # search API: 30 req/min authenticated
                time.sleep(20 * (attempt + 1))
                continue
            raise
    return {}


def collect(today: dt.date) -> list[dict]:
    since = (today - dt.timedelta(days=C.LOOKBACK_DAYS)).isoformat()
    queries = [f"created:>={since} stars:>={C.MIN_STARS}"]
    queries += [f"{t} created:>={since} stars:>={max(50, C.MIN_STARS // 3)}" for t in C.TOPIC_QUERIES]
    seen, out = set(), []
    for q in queries:
        data = gh_get("/search/repositories", {"q": q, "sort": "stars", "order": "desc", "per_page": C.PER_QUERY})
        for it in data.get("items", []):
            k = it["full_name"].lower()
            if k not in seen and not it.get("fork"):
                seen.add(k)
                out.append(it)
        time.sleep(2.2)
    return out


def readme_excerpt(full_name: str, limit: int = 4000) -> str:
    try:
        d = gh_get(f"/repos/{full_name}/readme")
        return base64.b64decode(d.get("content", "")).decode("utf-8", "replace")[:limit]
    except Exception:
        return ""


# ---------------- metrics (code) ----------------
def metrics(it: dict, today: dt.date) -> dict:
    created = dt.date.fromisoformat(it["created_at"][:10])
    pushed = dt.date.fromisoformat(it["pushed_at"][:10])
    age = max(1, (today - created).days)
    spd = it["stargazers_count"] / age
    fork_ratio = it["forks_count"] / max(1, it["stargazers_count"])
    lic = (it.get("license") or {}).get("spdx_id")
    return {
        "age_days": age,
        "stars_per_day": round(spd, 1),
        "fork_ratio": round(fork_ratio, 4),
        "days_since_push": max(0, (today - pushed).days),
        "license": lic if lic and lic != "NOASSERTION" else None,
        "padded_stars_flag": fork_ratio < C.PADDED_STARS_FORK_RATIO and spd >= C.PADDED_STARS_MIN_SPD,
    }


# ---------------- Jev (narrow judgments) ----------------
def build_questions() -> dict:
    from typesafe_sdk import Choice, Noul, Score

    q = {
        "category": Choice(
            instructions="Which category best describes what `repo` is? Judge from `repo.description`, `repo.topics` and `repo.readme`.",
            criteria=C.CATEGORIES,
        ),
        "fit": Score(instructions="How useful is `repo` to people building AI products, agents or automations?", criteria=C.FIT_LEVELS),
        "maturity": Score(
            instructions="Judging only from `repo.readme` and `repo.description`, how finished is the software in `repo`?",
            criteria=C.MATURITY_LEVELS,
        ),
    }
    for k, (text, _) in C.COMMON_NOULS.items():
        q[k] = Noul(instructions=text)
    for checks in C.TYPE_CHECKS.values():
        for k, text in checks.items():
            q[k] = Noul(instructions=text)
    return q


def judge_all(cands: list[dict], client=None) -> None:
    """One Jev request per candidate. Pass a client for tests; otherwise one is built from TYPESAFE_API_KEY."""
    from typesafe_sdk import RetryPolicy, TypeSafeClient

    questions = build_questions()
    own = client is None
    if own:
        client = TypeSafeClient(model=C.JEV_MODEL, retry=RetryPolicy(max_retries=4))
    try:
        for c in cands:
            state = {"repo": {  # untrusted text kept in its own named fields (readme, description)
                "name": c["slug"], "description": c["description"] or "", "topics": c["topics"],
                "language": c["language"] or "", "homepage": c["homepage"] or "", "readme": c["readme"]}}
            r = client.system_one(state=state, questions=questions)
            cat = r.choices["category"]
            ranked = sorted(cat.probabilities.items(), key=lambda kv: -kv[1])
            c["jev"] = {
                "model": r.model, "input_tokens": r.usage.input_tokens,
                "category": cat.choice, "category_confidence": cat.confidence,
                "category_runner_up": ranked[1][0] if len(ranked) > 1 else None,
                "fit": r.scores["fit"].score, "maturity": r.scores["maturity"].score,
                "nouls": {k: a.noul for k, a in r.nouls.items()},
            }
    finally:
        if own:
            client.close()


# ---------------- composite score (code) ----------------
MULTI_AGENT_CATEGORIES = ("Agent skills & plugins", "Design & UI", "Coding agents & dev tools")
REPLACES_PAID_CATEGORIES = ("Self-hosted business apps", "Voice & audio", "Personal productivity & utilities", "Marketing, SEO & content")


def score(c: dict, profiles: dict) -> None:
    m, j = c["metrics"], c.get("jev")
    cat = j["category"] if j else "unjudged"
    w = dict(C.WEIGHTS)
    w.update(C.CATEGORY_WEIGHT_OVERRIDES.get(cat, {}))
    med = profiles["categories"].get(cat, {}).get("median_stars_per_day") or profiles["overall"]["median_stars_per_day"]["single"]
    upkeep_span = C.UPKEEP_ZERO_DAYS - C.UPKEEP_FULL_DAYS
    parts = {
        "momentum": min(m["stars_per_day"] / max(med * C.MOMENTUM_BASELINE_MULT, 1), C.MOMENTUM_CAP) / C.MOMENTUM_CAP,
        "upkeep": 1.0 if m["days_since_push"] <= C.UPKEEP_FULL_DAYS else max(0.0, 1 - (m["days_since_push"] - C.UPKEEP_FULL_DAYS) / upkeep_span),
        "license": 1.0 if m["license"] else 0.0,
    }
    if j:
        parts["fit"] = j["fit"] / (len(C.FIT_LEVELS) - 1)
        parts["maturity"] = j["maturity"] / (len(C.MATURITY_LEVELS) - 1)
        vals = [j["nouls"][k] for k in C.TYPE_CHECKS.get(cat, {})]
        if cat in MULTI_AGENT_CATEGORIES:
            vals.append(j["nouls"]["multi_agent_host"])
        if cat in REPLACES_PAID_CATEGORIES:
            vals.append(j["nouls"]["replaces_paid"])
        parts["type_checks"] = sum(vals) / len(vals) if vals else 0.5
    used = {k: v for k, v in w.items() if k in parts}
    total = sum(parts[k] * v for k, v in used.items()) / sum(used.values())

    flags = []
    if m["padded_stars_flag"]:
        flags.append("padded stars?")
        total *= 0.8
    if j and j["nouls"]["injection"] > C.INJECTION_BLOCK:
        flags.append("injection text in README")
    if j and j["category_confidence"] < C.CATEGORY_MIN_CONFIDENCE:
        flags.append(f"category unsure (maybe {j['category_runner_up']})")
    if j and j["nouls"]["is_list"] > 0.5 and cat != "Learning resources & lists":
        flags.append("looks like a list")
    if j and cat == "other":
        flags.append("off-topic")

    c["score"] = round(total, 3)
    c["score_parts"] = {k: round(v, 3) for k, v in parts.items()}
    c["flags"] = flags
    blocked = cat == "other" or any(f.startswith(("injection", "off-topic")) for f in flags)
    if not j or blocked:  # no Jev judgment, or a gate tripped: never auto-keep
        c["decision"] = "review" if total >= C.REVIEW_SCORE else "skip"
    else:
        good_enough = (j["fit"] >= C.KEEP_MIN_FIT and j["maturity"] >= C.KEEP_MIN_MATURITY
                       and "looks like a list" not in flags and not m["padded_stars_flag"])
        c["decision"] = "keep" if total >= C.KEEP_SCORE and good_enough else "review" if total >= C.REVIEW_SCORE else "skip"


# ---------------- output ----------------
def digest(cands: list[dict], today: dt.date, dry_run: bool) -> str:
    keep = [c for c in cands if c["decision"] == "keep"]
    review = [c for c in cands if c["decision"] == "review"]
    lines = [f"# Repo scout, {today.isoformat()}", "",
             f"{len(cands)} new candidates, {len(keep)} kept, {len(review)} to review."
             + (" Dry run: no Jev judgments, code signals only." if dry_run else ""), ""]
    for title, group in (("Kept", keep[: C.DIGEST_TOP_N]), ("Worth a look", review[: C.DIGEST_TOP_N])):
        if not group:
            continue
        lines += [f"## {title}", ""]
        for c in group:
            cat = c.get("jev", {}).get("category", "unjudged")
            lines.append(f"- **[{c['slug']}]({c['url']})** · {cat} · ★{c['stars']} ({c['metrics']['stars_per_day']}/day) · score {c['score']}"
                         + (f" · {', '.join(c['flags'])}" if c["flags"] else "") + f"\n  {c['description'] or ''}")
        lines.append("")
    return "\n".join(lines)


def db_record(c: dict, today: dt.date) -> dict:
    return {"slug": c["slug"], "url": c["url"], "labels": [], "moved_from": [], "link_source": "scout",
            "found_on_github": True, "description": c["description"], "stars": c["stars"], "forks": c["forks"],
            "language": c["language"], "license": c["metrics"]["license"], "topics": c["topics"],
            "last_push": c["last_push"], "created": c["created"], "archived": False, "homepage": c["homepage"],
            "times_featured": 0, "first_featured": None, "episodes": [], "category": c["jev"]["category"],
            "tags": c["topics"][:4], "summary": c["description"], "replaces": None, "is_new": True,
            "why_fit": f"Scout score {c['score']} ({', '.join(f'{k} {v}' for k, v in c['score_parts'].items())})",
            "discovery_type": "Scout", "found_on": today.isoformat()}


def run(argv: list[str] | None = None, client=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=str(ROOT / "data" / "repos.json"))
    ap.add_argument("--profiles", default=str(ROOT / "data" / "success_profiles.json"))
    ap.add_argument("--out", default=str(ROOT / "data" / "runs"))
    ap.add_argument("--dry-run", action="store_true", help="skip Jev; nothing is auto-kept")
    ap.add_argument("--no-write", action="store_true", help="don't modify repos.json")
    ap.add_argument("--candidates-file", help="saved GitHub search items instead of calling GitHub (tests)")
    ap.add_argument("--today", help="override today's date, YYYY-MM-DD (tests)")
    a = ap.parse_args(argv)

    load_dotenv()
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    if not a.dry_run and client is None and not os.environ.get("TYPESAFE_API_KEY"):
        sys.exit("TYPESAFE_API_KEY is not set. Add it to .env (local) or as a repository secret (Actions), or use --dry-run.")

    db = json.loads(Path(a.db).read_text())
    profiles = json.loads(Path(a.profiles).read_text())
    have = {r["slug"].lower() for r in db["repos"]} | {s.lower() for r in db["repos"] for s in r.get("moved_from", [])}
    items = json.loads(Path(a.candidates_file).read_text()) if a.candidates_file else collect(today)
    items = [it for it in items if it["full_name"].lower() not in have and not it.get("archived")]
    items.sort(key=lambda it: -it["stargazers_count"])
    items = items[: C.MAX_CANDIDATES]

    cands = [{"slug": it["full_name"], "url": it["html_url"], "description": it.get("description"),
              "topics": it.get("topics") or [], "language": it.get("language"), "homepage": it.get("homepage"),
              "stars": it["stargazers_count"], "forks": it["forks_count"], "created": it["created_at"][:10],
              "last_push": it["pushed_at"][:10], "metrics": metrics(it, today),
              "readme": "" if a.candidates_file else readme_excerpt(it["full_name"])}
             for it in items]
    if not a.dry_run:
        judge_all(cands, client=client)
    for c in cands:
        score(c, profiles)
    cands.sort(key=lambda c: -c["score"])

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    run_file = out / f"{today.isoformat()}.json"
    run_file.write_text(json.dumps({"date": today.isoformat(), "dry_run": a.dry_run,
                                    "candidates": [{k: v for k, v in c.items() if k != "readme"} for c in cands]}, indent=1))
    (out / "digest.md").write_text(digest(cands, today, a.dry_run))

    keep = [c for c in cands if c["decision"] == "keep"]
    if keep and not a.no_write and not a.dry_run:
        db["repos"].extend(db_record(c, today) for c in keep)
        db["generated"] = today.isoformat()
        Path(a.db).write_text(json.dumps(db, indent=1))
    summary = {"candidates": len(cands), "keep": len(keep), "review": sum(c["decision"] == "review" for c in cands), "run_file": str(run_file)}
    print(f"{summary['candidates']} candidates · {summary['keep']} keep · {summary['review']} review · wrote {run_file}")
    return summary


if __name__ == "__main__":
    run()
