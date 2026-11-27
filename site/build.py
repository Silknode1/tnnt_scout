"""Build the static GitHub Pages site from data/repos.json.

usage: python site/build.py [--db data/repos.json] [--out _site]
Writes _site/index.html (data embedded, no fetch needed) plus copies of the
JSON and CSV so people can download the raw database.
"""

import argparse
import datetime as dt
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ["slug", "url", "category", "tags", "summary", "description", "replaces", "why_fit", "stars", "last_push",
          "found_on_github", "times_featured", "link_source", "moved_from", "labels", "episodes", "is_new"]
N_VIDEOS_READ = 174  # uploads read when the index was first built (Nov 16, 2026)


def pretty_date(iso: str) -> str:
    d = dt.date.fromisoformat(iso[:10])
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def build(db_path: Path, out_dir: Path) -> Path:
    db = json.loads(db_path.read_text())
    repos = db["repos"]
    rows = [{k: r.get(k) for k in FIELDS} for r in repos]
    featured = [r for r in repos if not r.get("is_new")]
    episodes = {e["video_id"] for r in featured for e in (r.get("episodes") or [])}
    counts = {
        "__NVIDEOS__": str(N_VIDEOS_READ),
        "__ASOF__": pretty_date(db.get("generated") or dt.date.today().isoformat()),
        "__NCOVERED__": str(len(featured)),
        "__NEPS__": str(len(episodes)),
        "__NREPEAT__": str(sum(1 for r in featured if (r.get("times_featured") or 0) >= 2)),
        "__NNEW__": str(len(repos) - len(featured)),
    }
    html = (ROOT / "site" / "template.html").read_text()
    for k, v in counts.items():
        html = html.replace(k, v)
    # "</" inside JSON would close the <script> tag early
    data = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace("__DATA__", data)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(html)
    for name in ("repos.json", "repos.csv"):
        src = db_path.parent / name
        if src.exists():
            shutil.copy(src, out_dir / name)
    (out_dir / ".nojekyll").write_text("")
    return out_dir / "index.html"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=str(ROOT / "data" / "repos.json"))
    p.add_argument("--out", default=str(ROOT / "_site"))
    a = p.parse_args(argv)
    path = build(Path(a.db), Path(a.out))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
