# TNNT repo scout

Finds new GitHub repos every day and judges each one by what kind of tool it is, using the
index of repos featured on The Next New Thing (`data/repos.json`) as the picture of what good
looks like (`data/success_profiles.json`).

## How a repo is judged

- **Code measures** stars per day against that category's norm, how recently it was updated,
  its license, and a padded-stars check (fast stars, almost no forks).
- **Jev (TypeSafe)** answers narrow questions in one call per repo: which of 18 categories it
  is, how useful it is to people building with AI, how finished it is, and two or three yes/no
  checks specific to its type. A token saver must state a measured saving, a self-hosted app
  should offer Docker, a skill pack needs a one-line install, and so on.
- **Code combines** them with per-category weights (`scout/constants.py`). A repo is kept only
  if the score clears 0.70 and Jev rates it useful and working. Promising repos that miss go to
  "Worth a look" in `data/runs/digest.md`. Prompt-injection text in a README blocks auto-keeping.

## Setup

1. Add a repository secret named `TYPESAFE_API_KEY` (Settings → Secrets and variables → Actions).
2. Run the `repo-scout` workflow once with **Run workflow** and read `data/runs/digest.md`.
3. Leave the daily schedule on (7am Central), or turn it off in the workflow file.
4. Turn on the site: Settings → Pages → Source: **GitHub Actions**. The `Deploy site`
   workflow publishes `site/` after every merge to main and after each successful scout run.

## The site

`python site/build.py` turns `data/repos.json` into one static page in `_site/` (search,
category and tag filters, sortable columns, new finds marked), plus downloadable JSON and CSV.
Nothing on the page calls TypeSafe, so the key never reaches the browser.

## Local use

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # then put your key in .env; git ignores it
python scout/scout.py --dry-run   # no key needed, nothing is kept
python scout/scout.py             # live run
pytest -q && ruff check .         # offline tests, Jev mocked
```

The key is read only from the environment (or `.env`). The code never prints or passes it.

## Files

- `scout/constants.py`: everything to tune (sources, categories, Jev questions, weights, gates)
- `scout/scout.py`: the pipeline
- `site/`: the page template and `build.py` for GitHub Pages
- `data/`: the repo index, episode list, success profiles, and scout runs
- `.github/workflows/`: `scout.yml` (daily run), `pages.yml` (site deploy) and `ci.yml` (tests on every PR)
