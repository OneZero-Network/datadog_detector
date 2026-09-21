#!/usr/bin/env python3
"""
Disposable research script v3: orgs that NEWLY added Datadog tracing SDKs.
History via local git clone + `git log -S` (exact first appearance).

Usage:
  export GITHUB_TOKEN=ghp_...
  pip install requests
  python datadog_detector_v3.py --days 14 --max-repos 1000 --pages 10
"""
import argparse, csv, os, re, subprocess, sys, tempfile, time
from datetime import datetime, timedelta, timezone
import requests

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN")
if not TOKEN:
    sys.exit("Set GITHUB_TOKEN (code search requires authentication).")
S = requests.Session()
S.headers.update({"Authorization": f"Bearer {TOKEN}",
                  "Accept": "application/vnd.github+json",
                  "X-GitHub-Api-Version": "2022-11-28"})

TARGETS = [
    ("dd-trace", "package.json", '"dd-trace":'),
    ("ddtrace", "requirements.txt", "ddtrace"),
    ("dd-trace-go", "go.mod", "github.com/DataDog/dd-trace-go"),
    ("ddtrace", "Gemfile", "ddtrace"),
]
EXCLUDED = re.compile(r"(^|[/_.-])(fixtures?|tests?|examples?|demos?|benchmarks?)([/_.-]|$)", re.I)
VENDOR_OWNERS = {"datadog", "datadog-labs"}
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def get(url, **params):
    r = None
    for _ in range(5):
        r = S.get(url if url.startswith("http") else API + url, params=params)
        if r.status_code in (403, 429):
            reset, wait = r.headers.get("X-RateLimit-Reset"), r.headers.get("Retry-After")
            if wait: sleep = int(wait) + 1
            elif r.headers.get("X-RateLimit-Remaining") == "0" and reset:
                sleep = max(int(reset) - int(time.time()), 1) + 1
            else: sleep = 30
            print(f"  rate limited, sleeping {sleep}s", file=sys.stderr)
            time.sleep(min(sleep, 120)); continue
        return r
    return r


def git(args, cwd=None, timeout=180):
    """Never raises on timeout; returns object with returncode/stdout/stderr."""
    try:
        return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True,
                              timeout=timeout, env=GIT_ENV)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "timeout")


def first_appearance(repo, path, needle, depth=200):
    """Returns (sha, iso_date, is_root, verified). verified=True only if full
    history was considered for the answer (never trusts a shallow boundary)."""
    with tempfile.TemporaryDirectory(prefix="ddd-") as tmp:
        d = os.path.join(tmp, "r")
        c = git(["clone", "--depth", str(depth), "--filter=blob:none", "--no-checkout",
                 "--single-branch", f"https://github.com/{repo}.git", d], timeout=300)
        if c.returncode != 0:
            print(f"  clone failed {repo}: {c.stderr.strip()[:200]}", file=sys.stderr)
            return None, None, False, False

        def search():
            r = git(["log", "--reverse", "--format=%H", "-S", needle, "--", path], cwd=d, timeout=300)
            shas = [x for x in r.stdout.split() if x] if r.returncode == 0 else None
            return shas

        def shallow_set():
            p = os.path.join(d, ".git", "shallow")
            return set(open(p).read().split()) if os.path.exists(p) else set()

        shas = search()
        if shas is None:
            return None, None, False, False
        # The oldest match inside a shallow window is a fake "addition"
        # (boundary commit diffs against empty). Unshallow and redo.
        if shallow_set() and (not shas or shas[0] in shallow_set()):
            print(f"  unshallowing {repo}...", flush=True)
            f = git(["fetch", "--unshallow", "--filter=blob:none"], cwd=d, timeout=900)
            if f.returncode != 0:
                print(f"  unshallow failed {repo}: {f.stderr.strip()[:200]}", file=sys.stderr)
                return None, None, False, False
            shas = search()
            if shas is None:
                return None, None, False, False
        if not shas:
            return None, None, False, True
        sha = shas[0]
        date = git(["show", "-s", "--format=%cI", sha], cwd=d, timeout=60).stdout.strip()
        roots = set(git(["rev-list", "--max-parents=0", sha], cwd=d, timeout=60).stdout.split())
        return sha, date, sha in roots, bool(date)


def org_info(login):
    r = get(f"/orgs/{login}")
    return r.json() if r.status_code == 200 else {}


def contributors(repo):
    r = get(f"/repos/{repo}/contributors", per_page=30, anon="false")
    return len(r.json()) if r.status_code == 200 and isinstance(r.json(), list) else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--max-repos", type=int, default=1000)
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--clone-depth", type=int, default=200)
    ap.add_argument("--out", default="datadog_signals.csv")
    a = ap.parse_args()
    cutoff = datetime.now(timezone.utc) - timedelta(days=a.days)
    funnel = dict(hits=0, unique_repos=0, excluded_paths=0, org_owned=0, not_fork=0,
                  active=0, history_verified=0, added_recently=0,
                  added_to_existing_manifest=0, first_ever_in_repo=0, errors=0)
    seen = {}
    per_target = max(a.max_repos // len(TARGETS), 1)

    for term, fname, needle in TARGETS:
        count = 0
        for page in range(1, a.pages + 1):
            r = get("/search/code", q=f"{term} filename:{fname}", per_page=100, page=page)
            if r.status_code != 200:
                print(f"search failed {r.status_code}: {r.text[:200]}", file=sys.stderr); break
            items = r.json().get("items", [])
            if not items: break
            funnel["hits"] += len(items)
            for it in items:
                repo, path = it["repository"]["full_name"], it["path"]
                if EXCLUDED.search(path):
                    funnel["excluded_paths"] += 1; continue
                if (repo, path) not in seen and count < per_target:
                    seen[(repo, path)] = (term, needle); count += 1
            if count >= per_target: break
            time.sleep(7)
    funnel["unique_repos"] = len(seen)

    writer = out_f = None
    for (repo, path), (term, needle) in sorted(seen.items()):
        try:
            if repo.split("/")[0].lower() in VENDOR_OWNERS: continue
            mr = get(f"/repos/{repo}")
            if mr.status_code != 200: continue
            meta = mr.json()
            if meta.get("owner", {}).get("type") != "Organization": continue
            funnel["org_owned"] += 1
            if meta.get("fork") or meta.get("archived"): continue
            funnel["not_fork"] += 1
            if datetime.fromisoformat(meta["pushed_at"].replace("Z", "+00:00")) < cutoff: continue
            funnel["active"] += 1

            sha, date, is_root, verified = first_appearance(repo, path, needle, a.clone_depth)
            if not verified or not sha: continue
            funnel["history_verified"] += 1
            when = datetime.fromisoformat(date)
            if when < cutoff: continue
            funnel["added_recently"] += 1
            funnel["first_ever_in_repo" if is_root else "added_to_existing_manifest"] += 1

            org = org_info(meta["owner"]["login"])
            created = datetime.fromisoformat(meta["created_at"].replace("Z", "+00:00"))
            row = dict(
                organization=meta["owner"]["login"], org_name=org.get("name") or "",
                company_url=org.get("blog") or "", org_public_repos=org.get("public_repos", ""),
                repository=repo, manifest=path, dependency=term,
                first_detected=when.date().isoformat(),
                added_to_existing_manifest=not is_root, first_ever_in_repo=is_root,
                history_verified=True,
                commit_url=f"https://github.com/{repo}/commit/{sha}",
                manifest_url=f"https://github.com/{repo}/blob/{meta['default_branch']}/{path}",
                contributors=contributors(repo),
                repo_age_days=(datetime.now(timezone.utc) - created).days,
                stars=meta.get("stargazers_count", 0),
                description=(meta.get("description") or "")[:120],
                manual_credible_company="", manual_real_adoption="",
                manual_existing_datadog_customer="", manual_reason="")
            if writer is None:
                out_f = open(a.out, "w", newline="", encoding="utf-8")
                writer = csv.DictWriter(out_f, fieldnames=row.keys()); writer.writeheader()
            writer.writerow(row); out_f.flush()
            print(f"  + {repo} ({when.date()}) [{'repo-root' if is_root else 'existing repo'}]", flush=True)
        except Exception as e:
            funnel["errors"] += 1
            print(f"  ! skipped {repo}: {e!r}", file=sys.stderr)

    print("\nFUNNEL")
    for k, v in funnel.items(): print(f"  {k:32s} {v}")
    print(f"\nRows written: see {a.out}. Inspect EVERY row by hand.")


if __name__ == "__main__":
    main()
