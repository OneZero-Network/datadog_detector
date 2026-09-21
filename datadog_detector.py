#!/usr/bin/env python3
"""
Disposable research script: find orgs that NEWLY added Datadog tracing SDKs
to public repos. Goal: 20 credible examples, then manually inspect.

Usage:
  export GITHUB_TOKEN=ghp_...   (classic or fine-grained, public read is enough)
  pip install requests
  python datadog_detector.py --days 14 --max-repos 150

Output: datadog_signals.csv  + printed funnel (your key metric).
"""
import argparse, base64, csv, os, re, sys, time
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

# (search term, manifest filename, regex proving dependency present in content)
TARGETS = [
    ("dd-trace", "package.json", re.compile(r'"dd-trace"\s*:')),
    ("ddtrace", "requirements.txt", re.compile(r'(?im)^\s*ddtrace\b')),
    ("dd-trace-go", "go.mod", re.compile(r'DataDog/dd-trace-go')),
    ("ddtrace", "Gemfile", re.compile(r"(?i)gem\s+['\"](ddtrace|datadog)['\"]")),
]


def get(url, **params):
    """GET with rate-limit handling."""
    for _ in range(5):
        r = S.get(url if url.startswith("http") else API + url, params=params)
        if r.status_code in (403, 429):
            reset = r.headers.get("X-RateLimit-Reset")
            wait = r.headers.get("Retry-After")
            if wait:
                sleep = int(wait) + 1
            elif r.headers.get("X-RateLimit-Remaining") == "0" and reset:
                sleep = max(int(reset) - int(time.time()), 1) + 1
            else:
                sleep = 30
            print(f"  rate limited, sleeping {sleep}s", file=sys.stderr)
            time.sleep(min(sleep, 120))
            continue
        return r
    return r


def file_at(repo, path, ref):
    r = get(f"/repos/{repo}/contents/{path}", ref=ref)
    if r.status_code != 200:
        return None
    j = r.json()
    if isinstance(j, dict) and j.get("content"):
        return base64.b64decode(j["content"]).decode("utf-8", "ignore")
    return None


def first_appearance(repo, path, pattern, max_probe=12):
    """Walk commits touching the manifest newest->oldest until dep is absent.
    Returns (first_commit_dict or None, existed_before: bool, exhausted: bool)."""
    r = get(f"/repos/{repo}/commits", path=path, per_page=max_probe)
    if r.status_code != 200 or not r.json():
        return None, False, False
    commits = r.json()
    earliest_with = None
    for i, c in enumerate(commits):
        content = file_at(repo, path, c["sha"])
        if content is not None and pattern.search(content):
            earliest_with = c
            continue
        # dependency absent at this commit -> earliest_with is where it was added
        return earliest_with, content is not None, False
    # never found absent within probe window: history longer than probe, or file born with dep
    exhausted = len(commits) == max_probe
    return earliest_with, False, exhausted


def org_info(login):
    r = get(f"/orgs/{login}")
    return r.json() if r.status_code == 200 else {}


def contributors(repo):
    r = get(f"/repos/{repo}/contributors", per_page=30, anon="false")
    return len(r.json()) if r.status_code == 200 and isinstance(r.json(), list) else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--max-repos", type=int, default=1000, help="total candidate cap, split evenly across targets")
    ap.add_argument("--pages", type=int, default=3, help="search pages (100 hits each) per target")
    ap.add_argument("--out", default="datadog_signals.csv")
    a = ap.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(days=a.days)
    funnel = dict(hits=0, unique_repos=0, org_owned=0, not_fork=0, active=0,
                  added_recently=0, history_verified=0, added_to_existing_manifest=0)
    seen, rows = {}, []

    per_target = max(a.max_repos // len(TARGETS), 1)
    for term, fname, pat in TARGETS:
        count = 0
        for page in range(1, a.pages + 1):
            r = get("/search/code", q=f"{term} filename:{fname}", per_page=100, page=page)
            if r.status_code != 200:
                print(f"search failed {r.status_code}: {r.text[:200]}", file=sys.stderr)
                break
            items = r.json().get("items", [])
            if not items:
                break
            funnel["hits"] += len(items)
            for it in items:
                key = (it["repository"]["full_name"], it["path"])
                if key not in seen and count < per_target:
                    seen[key] = (term, pat); count += 1
            if count >= per_target:
                break
            time.sleep(7)  # code search ~10 req/min: pace per REQUEST, not per hit
    funnel["unique_repos"] = len(seen)

    out_f, writer = None, None
    for (repo, path), (dep, pat) in sorted(seen.items()):
      try:
        if repo.split('/')[0].lower() in ('datadog', 'datadog-labs'):
            continue
        meta = get(f"/repos/{repo}").json()
        if meta.get("owner", {}).get("type") != "Organization":
            continue
        funnel["org_owned"] += 1
        if meta.get("fork") or meta.get("archived"):
            continue
        funnel["not_fork"] += 1
        pushed = datetime.fromisoformat(meta["pushed_at"].replace("Z", "+00:00"))
        if pushed < cutoff:
            continue
        funnel["active"] += 1

        first, existed, exhausted = first_appearance(repo, path, pat)
        if not first:
            continue
        when = datetime.fromisoformat(first["commit"]["committer"]["date"].replace("Z", "+00:00"))
        if when < cutoff:
            continue
        funnel["added_recently"] += 1
        if not exhausted:
            funnel["history_verified"] += 1
        if existed:
            funnel["added_to_existing_manifest"] += 1

        org = org_info(meta["owner"]["login"])
        created = datetime.fromisoformat(meta["created_at"].replace("Z", "+00:00"))
        rows.append(dict(
            organization=meta["owner"]["login"],
            org_name=org.get("name") or "",
            company_url=org.get("blog") or "",
            org_public_repos=org.get("public_repos", ""),
            repository=repo,
            manifest=path,
            dependency=dep,
            first_detected=when.date().isoformat(),
            previously_present_manifest=existed,  # True = dep added to a pre-existing file
            history_truncated=exhausted,
            commit_url=first["html_url"],
            manifest_url=f"https://github.com/{repo}/blob/{meta['default_branch']}/{path}",
            contributors=contributors(repo),
            repo_age_days=(datetime.now(timezone.utc) - created).days,
            stars=meta.get("stargazers_count", 0),
            description=(meta.get("description") or "")[:120],
            history_verified=not exhausted,  # only these count toward the 20
            manual_credible_company="", manual_real_adoption="",
            manual_existing_datadog_customer="", manual_reason="",
        ))
        print(f"  + {repo} ({when.date()})", flush=True)
        if writer is None:
            out_f = open(a.out, "w", newline="")
            writer = csv.DictWriter(out_f, fieldnames=rows[-1].keys()); writer.writeheader()
        writer.writerow(rows[-1]); out_f.flush()
      except Exception as e:
        print(f"  ! skipped {repo}: {e!r}", file=sys.stderr)


    print("\nFUNNEL (your key metric = last rows / first rows)")
    for k, v in funnel.items():
        print(f"  {k:32s} {v}")
    print(f"\nWrote {len(rows)} rows to {a.out}. Now inspect EVERY row by hand and mark:")
    print("  credible company? real adoption (not tutorial/demo)? already-known to vendor?")


if __name__ == "__main__":
    main()
