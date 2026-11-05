"""Weekly PR and issue pulse, one JSON file per product.

Standalone: one file, Python 3.10+ standard library only, plus the GitHub CLI (`gh`).

Give it a JSON file that maps each product name to the GitHub repos behind it:

    {
      "appevolve": ["ITP-INC/appevolve-backend", "ITP-INC/appevolve-frontend"],
      "prodtwin": ["ITP-INC/prodtwin-stable"]
    }

For each product it reads one week of pull requests and issues from every one of its repos,
pools them, and writes `<product>-pulse.json`: volume and velocity, who did what, functional
areas, hubs and environments, production-readiness progress and the open backlog, plus a
per-repo breakdown. Every listed PR and issue carries a `repo`, because numbers repeat
across repos.

Set up once:
  1. Install Python 3.10+ and the GitHub CLI (https://cli.github.com).
  2. Log gh in with read access to every repo:
         gh auth login --hostname intuitive-technology-partners-inc.ghe.com
     If gh knows several hosts, set GH_HOST to that one so `OWNER/REPO` resolves there.

Run (dates are inclusive UTC days; Sunday to Saturday is the usual week):

    python weekly_pr_pulse.py --products products.json --start 2026-10-25 --end 2026-10-31
    python weekly_pr_pulse.py --products products.json --start 2026-10-25 --end 2026-10-31 --only prodtwin --out-dir pulses

Files go to --out-dir (default: the current folder). It takes a few minutes per repo. If gh
cannot read a repo, that product reports the repo, writes no file and the run exits 1; the
other products are still written, so a report never leaves a repo out silently.

Things to know:
  * Deploy counts exist only for ITP-INC/SemantixIQ-M's fleet (its `ops/fleet-deploy-briefs/*`
    PRs and the briefs in S3). Any other product reports 0 deploys with
    `volume.deploys_complete: false` and a note. The S3 part needs the `aws` CLI with access
    (--aws-profile); without it the counts are marked incomplete instead of failing. Pass
    --briefs-s3 "" to skip S3.
  * Functional areas and hubs are keyword rules on PR titles (the AREAS and HUBS lists below).
    They were written for SemantixIQ-M's work, so another product's PRs mostly land in
    "Other" until you add rules for it.
  * --lead splits one GitHub login out of the "team" counts; --lead "" treats everyone as team.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
import subprocess
from pathlib import Path

REPO = "ITP-INC/SemantixIQ-M"  # the repo whose fleet writes the deploy briefs
# Briefs went to one PR per deploy until 25 Oct 2026 (#3497), and to S3 since.
FLEET_BRANCH = "ops/fleet-deploy-briefs/"
BRIEFS_S3 = "s3://intuitiveiq-demo/deploy-briefs/"

# First match wins, so put the most specific rules first.
AREAS = [
    ("UCL POC", r"\bucl\b"),
    ("AutoMap", r"automap|crosswalk|answer key|\brc-\d|word list"),
    (
        "Ontology views & three-layer",
        r"ontology.views?|\bov-?\d|\bov\d|\b3l\b|3l\(|three.layer",
    ),
    (
        "Security & identity",
        (
            r"secur|\bauth|rbac|saml|oidc|secret|ssrf|credential|keycloak|pentest|break.glass|password"
            r"|dprod.*manage|other-client|neutral|noindex|rotate"
        ),
    ),
    ("Value benchmark", r"benchmark|vantora|parity"),
    ("Docs", r"^docs|docs\(|docs-site|guide\b|runbook|readme|claude\.md|handoff"),
    (
        "Client hub delivery",
        (
            r"kcpg|kenvue|kv tables|hcp_provider_full|hcp clone|key.evidence|acceptance"
            r"|hcp-fabric|hcprovider|go-live"
        ),
    ),
    ("CPG Control Tower & insights", r"\bcpg\b|control.tower|industry.insights"),
    (
        "Calculation Registry & query",
        (
            r"calc|qig|nl2sparql|planner|sparql|agent.console|genie|query|exemplar|sum check"
            r"|agg_|grounding|partial answer|governed sql|compare"
        ),
    ),
    (
        "CI, tests & quality",
        r"\bci\b|ci\(|\btest|coverage|playwright|quarantine|lint|ruff|golden|qa\)|\bqa\b",
    ),
    ("Appliance & on-prem", r"applian|onprem|on-prem|sealed|rebrand|marketplace|licen"),
    (
        "Ops, Ontop & fleet",
        (
            r"fleet|deploy|\bops\b|ops\(|obs\(|observab|grafana|backup|compose|ontop|docker|router"
            r"|llm|429|postflight|procpool|data-tier|caddy|models|saas_split"
        ),
    ),
    (
        "Hub build, mapping & data",
        (
            r"hub|mapping|synthetic|shacl|align|provision|build|r2rml|ontology|reconcile"
            r"|federation|interchange|data-product|action|\bG\d:|decision twin|fibo|banking"
        ),
    ),
    ("UI", r"\bui\b|ui\)|app\.html|console|screen|admin studio"),
]
HUBS = [
    ("Kenvue CPG (kcpg)", r"kcpg|kenvue|kvcpg|kenvue_cc|\bkc\b"),
    ("HCP Provider (Fabric)", r"hcprovider|hcp_provider|hcp-fabric|\bhcp\b|epic|fhir"),
    ("Vantora (finance)", r"vantora"),
    (
        "Financial services hubs",
        r"bfsi|banking|finserv|meridian|lattice|global_banking|fibo",
    ),
    ("Siemens PLM", r"siemens"),
    ("Healthcare payer / BHSF", r"hc_payer|hcpayer|bhsf"),
    ("CPG / other verticals", r"\bcpg|steel|copper|homecare|hc_homecare"),
]
SEVERITY = ("P0", "P1", "P2", "P3")
# report key -> issue label
READINESS_LABELS = {
    "prod_readiness": "prod-readiness",
    "P0": "P0",
    "P1": "P1",
    "quality_sprint": "sprint:stop-the-bleeding",
}
ISSUE_COUNTS = ("open", "closed", "closed_this_week", "opened_this_week")
TREND_WEEKS_BACK = range(5, -1, -1)  # oldest first, ending with the report week


class GhError(RuntimeError):
    """`gh` is missing or failed for one repo; the product being read cannot be reported."""


def product_name(s: str) -> str:
    """The product becomes a file name, so keep it to letters, digits, '-' and '_'."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", s):
        raise ValueError(
            f"{s!r}: use letters, digits, '-' and '_' (it becomes the file name)"
        )
    return s


def repo_name(s: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", s):
        raise ValueError(f"{s!r}: expected OWNER/REPO")
    return s


def unique_repos(repos: list[str]) -> list[str]:
    """Drop repeats (GitHub names are case-insensitive), keeping the first spelling and order."""
    seen: set[str] = set()
    return [r for r in repos if not (r.lower() in seen or seen.add(r.lower()))]


def parse_products(text: str, source: str) -> dict[str, list[str]]:
    """Read `{"<product>": ["OWNER/REPO", ...], ...}` into product -> repos.

    Raises ValueError naming `source` and the product at fault. A product listed twice is an
    error (JSON would silently keep the last one and drop repos), and names that differ only
    in case are the same product, since they would write the same file on Windows or macOS.
    """
    example = (
        '{"appevolve": ["ITP-INC/appevolve-backend", "ITP-INC/appevolve-frontend"]}'
    )

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict:
        found: dict[str, object] = {}
        for key, value in pairs:
            if key.lower() in {k.lower() for k in found}:
                raise ValueError(
                    f"{source}: product {key!r} appears twice (names are compared ignoring case)"
                )
            found[key] = value
        return found

    try:
        raw = json.loads(text, object_pairs_hook=no_duplicates)
    except json.JSONDecodeError as e:
        raise ValueError(f"{source}: not valid JSON ({e})") from e
    if not isinstance(raw, dict) or not raw:
        raise ValueError(
            f"{source}: expected a JSON object of product name to list of repos, e.g. {example}"
        )
    products: dict[str, list[str]] = {}
    for name, repos in raw.items():
        try:
            product_name(name)
            if not isinstance(repos, list) or not repos:
                raise ValueError("needs a non-empty list of OWNER/REPO strings")
            if not all(isinstance(r, str) for r in repos):
                raise ValueError("every repo must be an OWNER/REPO string")
            products[name] = unique_repos([repo_name(r) for r in repos])
        except ValueError as e:
            raise ValueError(f"{source}: product {name!r}: {e}") from e
    return products


def briefs_source(repos: list[str], bucket: str) -> str | None:
    """Fleet deploy briefs are SemantixIQ-M's, so only a product that includes that repo reads
    `bucket`; an empty bucket turns the lookup off."""
    return bucket if bucket and REPO.lower() in {r.lower() for r in repos} else None


def run_gh(repo: str, cmd: list[str]) -> list | dict:
    """Run `gh` and parse its JSON. Raises GhError naming `repo` if gh is missing or fails: a
    product with one unread repo must not turn into a report that looks complete."""
    try:
        out = subprocess.run(
            ["gh", *cmd],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
    except FileNotFoundError as e:
        raise GhError(
            "the GitHub CLI `gh` is not installed or not on PATH "
            "(install it from https://cli.github.com, then run `gh auth login`)"
        ) from e
    except subprocess.CalledProcessError as e:
        raise GhError(
            f"gh {' '.join(cmd[:2])} failed for {repo}: {(e.stderr or '').strip()}"
        ) from e
    return json.loads(out) if out.strip() else []


def gh(repo: str, *args: str) -> list:
    return run_gh(repo, [*args, "--repo", repo])


def repo_url(repo: str) -> str:
    return run_gh(repo, ["repo", "view", repo, "--json", "url"])["url"]


def prs(repo: str, query: str, fields: str, limit: int = 2000) -> list:
    return gh(
        repo,
        "pr",
        "list",
        "--state",
        "all",
        "--search",
        query,
        "--limit",
        str(limit),
        "--json",
        fields,
    )


def issues(repo: str, query: str, fields: str = "number", limit: int = 2000) -> list:
    return gh(
        repo,
        "issue",
        "list",
        "--state",
        "all",
        "--search",
        query,
        "--limit",
        str(limit),
        "--json",
        fields,
    )


def classify(title: str, rules, branch: str = "") -> str:
    """Titles decide; the branch name is only consulted when the title matches nothing."""
    for text in (title, branch):
        for name, rx in rules:
            if text and re.search(rx, text, re.IGNORECASE):
                return name
    return "Other"


def tags(title: str, rules) -> list[str]:
    return [name for name, rx in rules if re.search(rx, title, re.IGNORECASE)]


def deploy_env(branch: str) -> str:
    """`ops/fleet-deploy-briefs/<env>-<YYYYMMDD>-...`; env names can contain hyphens."""
    m = re.search(r"fleet-deploy-briefs/(.+?)-\d{8}-", branch)
    return m.group(1) if m else "unknown"


def brief_key(name: str) -> tuple[str, str] | None:
    """(`YYYYMMDD-HHMMSS`, env) from an S3 brief `<ts>-<env>.md` or a brief PR branch."""
    m = re.fullmatch(r"(\d{8}-\d{6})-(.+)\.md", name)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"fleet-deploy-briefs/(.+?)-(\d{8}-\d{6})", name)
    return (m.group(2), m.group(1)) if m else None


def s3_briefs(url: str, profile: str | None) -> list[str]:
    cmd = ["aws", "s3", "ls", url]
    if profile:
        cmd += ["--profile", profile]
    out = subprocess.run(
        cmd, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout
    return [line.split()[-1] for line in out.splitlines() if line.strip()]


def deploys_in_window(
    names: list[str], start: dt.date, end: dt.date
) -> list[tuple[str, str]]:
    """Unique (ts, env) deploys whose UTC day is in [start, end]; a brief seen twice counts once."""
    keys = {k for k in map(brief_key, names) if k}
    lo, hi = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    return sorted(k for k in keys if lo <= k[0][:8] <= hi)


def hours(a: str, b: str) -> float:
    f = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (f(b) - f(a)).total_seconds() / 3600


def label_names(p) -> list[str]:
    return [l["name"] for l in p.get("labels", [])]


def collect_repo(repo: str, start: dt.date, end: dt.date) -> dict:
    """One repo's raw data for the report. PR and issue numbers repeat across repos, so every
    record carries its `repo`."""
    rng = f"{start}..{end}"
    data = {
        "url": repo_url(repo),
        "merged_all": prs(
            repo,
            f"is:merged merged:{rng}",
            "number,title,author,createdAt,mergedAt,headRefName,labels,additions,deletions",
        ),
        "opened": prs(repo, f"created:{rng}", "number,author,headRefName"),
        "closed_unmerged": prs(
            repo, f"is:closed is:unmerged closed:{rng}", "number,title,author"
        ),
        "open_prs": gh(
            repo,
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            "2000",
            "--json",
            "number,title,author,createdAt,isDraft,labels,reviewRequests,headRefName",
        ),
        "open_p0": issues(repo, "is:open label:P0", "number,title,assignees,createdAt"),
    }
    for key in ("merged_all", "opened", "closed_unmerged", "open_prs", "open_p0"):
        for item in data[key]:
            item["repo"] = repo
    data["trend"] = [
        prs(
            repo,
            f"is:merged merged:{start - dt.timedelta(7 * w)}..{end - dt.timedelta(7 * w)}",
            "author,headRefName",
        )
        for w in TREND_WEEKS_BACK
    ]
    data["readiness"] = {
        key: {
            "open": len(issues(repo, f"is:open label:{label}")),
            "closed": len(issues(repo, f"is:closed label:{label}")),
            "closed_this_week": len(
                issues(repo, f"is:closed label:{label} closed:{rng}")
            ),
            "opened_this_week": len(issues(repo, f"label:{label} created:{rng}")),
        }
        for key, label in READINESS_LABELS.items()
    }
    return data


def build_report(
    product: str,
    repos: list[str],
    start: dt.date,
    end: dt.date,
    lead: str,
    fleet_briefs_s3: str,
    aws_profile: str | None,
) -> dict:
    """The week's report for one product, pooled over `repos`. Raises GhError if any repo cannot
    be read."""
    repo_data = [collect_repo(r, start, end) for r in repos]

    def pooled(key: str) -> list:
        return [item for d in repo_data for item in d[key]]

    merged_all = pooled("merged_all")
    deploy_prs = [p for p in merged_all if p["headRefName"].startswith(FLEET_BRANCH)]
    merged = [p for p in merged_all if not p["headRefName"].startswith(FLEET_BRANCH)]
    opened = [
        p for p in pooled("opened") if not p["headRefName"].startswith(FLEET_BRANCH)
    ]
    closed_unmerged = pooled("closed_unmerged")

    who = collections.Counter(p["author"]["login"] for p in merged)
    opened_by = collections.Counter(p["author"]["login"] for p in opened)
    team = [p for p in merged if p["author"]["login"] != lead]
    cycle = sorted(hours(p["createdAt"], p["mergedAt"]) for p in merged)
    team_cycle = sorted(hours(p["createdAt"], p["mergedAt"]) for p in team)

    def pct(xs, q):
        return round(xs[min(len(xs) - 1, int(len(xs) * q))], 1) if xs else None

    per_day = collections.Counter(p["mergedAt"][:10] for p in merged)
    days = [
        (start + dt.timedelta(d)).isoformat() for d in range((end - start).days + 1)
    ]

    trend = []
    for i, w in enumerate(TREND_WEEKS_BACK):
        ws = start - dt.timedelta(7 * w)
        wk = [
            p
            for d in repo_data
            for p in d["trend"][i]
            if not p["headRefName"].startswith(FLEET_BRANCH)
        ]
        trend.append(
            {
                "week": ws.isoformat(),
                "merged": len(wk),
                "lead": sum(p["author"]["login"] == lead for p in wk),
                "team": sum(p["author"]["login"] != lead for p in wk),
            }
        )

    areas = collections.defaultdict(list)
    for p in merged:
        areas[classify(p["title"], AREAS, p["headRefName"])].append(p)
    hubs = collections.defaultdict(list)
    for p in merged:
        for h in tags(p["title"] + " " + p["headRefName"], HUBS):
            hubs[h].append(p)

    brief_names = [p["headRefName"] for p in deploy_prs]
    deploys_note = None
    briefs_s3 = briefs_source(repos, fleet_briefs_s3)
    if briefs_s3 is None:
        deploys_note = (
            "no S3 deploy-brief source for this product; counts are PR briefs only"
        )
    else:
        try:
            brief_names += s3_briefs(briefs_s3, aws_profile)
        except (OSError, subprocess.CalledProcessError) as e:
            detail = getattr(e, "stderr", "") or str(e)
            deploys_note = f"S3 briefs not read ({detail.strip()[:200]}); counts are PR briefs only"
            print(f"warning: {deploys_note}")
    deploys = deploys_in_window(brief_names, start, end)
    env = collections.Counter(env for _, env in deploys)

    open_prs = [
        p for p in pooled("open_prs") if not p["headRefName"].startswith(FLEET_BRANCH)
    ]
    now = dt.datetime.now(dt.timezone.utc)
    age = lambda p: (
        (now - dt.datetime.fromisoformat(p["createdAt"].replace("Z", "+00:00"))).days
    )
    buckets = collections.Counter(
        "<7d"
        if age(p) < 7
        else "7-14d"
        if age(p) < 14
        else "14-30d"
        if age(p) < 30
        else "30d+"
        for p in open_prs
    )
    waiting = collections.Counter(
        r.get("login") for p in open_prs for r in p["reviewRequests"] if r.get("login")
    )
    high = [p for p in open_prs if set(label_names(p)) & {"P0", "P1"}]

    readiness = {
        key: {m: sum(d["readiness"][key][m] for d in repo_data) for m in ISSUE_COUNTS}
        for key in READINESS_LABELS
    }
    open_p0 = pooled("open_p0")
    report = {
        "product": product,
        "repo": ", ".join(repos),
        "repos": [
            {
                "repo": r,
                "url": d["url"],
                "merged": sum(p["repo"] == r for p in merged),
                "opened": sum(p["repo"] == r for p in opened),
                "closed_unmerged": sum(p["repo"] == r for p in closed_unmerged),
                "open_prs": sum(p["repo"] == r for p in open_prs),
                "lines_added": sum(p["additions"] for p in merged if p["repo"] == r),
                "lines_deleted": sum(p["deletions"] for p in merged if p["repo"] == r),
            }
            for r, d in zip(repos, repo_data)
        ],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "lead": lead,
        "url": repo_data[0]["url"],
        "generated_at": now.isoformat(timespec="minutes"),
        "volume": {
            "merged": len(merged),
            "opened": len(opened),
            "closed_unmerged": len(closed_unmerged),
            "deploys": len(deploys),
            "deploys_complete": deploys_note is None,
            "deploys_note": deploys_note,
            "lines_added": sum(p["additions"] for p in merged),
            "lines_deleted": sum(p["deletions"] for p in merged),
            "lead_merged": who.get(lead, 0),
            "team_merged": len(team),
        },
        "velocity": {
            "per_day": [{"day": d, "merged": per_day.get(d, 0)} for d in days],
            "cycle_hours": {
                "p50": pct(cycle, 0.5),
                "p75": pct(cycle, 0.75),
                "p90": pct(cycle, 0.9),
            },
            "team_cycle_hours": {
                "p50": pct(team_cycle, 0.5),
                "p90": pct(team_cycle, 0.9),
            },
            "trend": trend,
        },
        "people": [
            {
                "login": k,
                "merged": who.get(k, 0),
                "opened": opened_by.get(k, 0),
                "open_now": sum(p["author"]["login"] == k for p in open_prs),
                "waiting_on_their_review": waiting.get(k, 0),
            }
            for k in sorted(
                set(who) | set(opened_by), key=lambda k: (-who.get(k, 0), k)
            )
        ],
        "areas": [
            {
                "area": k,
                "merged": len(v),
                "authors": dict(collections.Counter(p["author"]["login"] for p in v)),
                "top": [
                    {
                        "number": p["number"],
                        "title": p["title"],
                        "author": p["author"]["login"],
                        "repo": p["repo"],
                    }
                    for p in sorted(
                        v, key=lambda p: -(p["additions"] + p["deletions"])
                    )[:6]
                ],
            }
            for k, v in sorted(areas.items(), key=lambda kv: -len(kv[1]))
        ],
        "hubs": [
            {
                "hub": k,
                "merged": len(v),
                "top": [
                    {"number": p["number"], "title": p["title"], "repo": p["repo"]}
                    for p in v[:5]
                ],
            }
            for k, v in sorted(hubs.items(), key=lambda kv: -len(kv[1]))
        ],
        "environments": [{"env": k, "deploys": v} for k, v in env.most_common()],
        "readiness": readiness,
        "backlog": {
            "open_prs": len(open_prs),
            "drafts": sum(p["isDraft"] for p in open_prs),
            "age": {k: buckets.get(k, 0) for k in ("<7d", "7-14d", "14-30d", "30d+")},
            "waiting_on_review": dict(waiting.most_common(8)),
            "severity": {
                s: sum(s in label_names(p) for p in open_prs) for s in SEVERITY
            },
            "oldest_high": [
                {
                    "number": p["number"],
                    "title": p["title"],
                    "author": p["author"]["login"],
                    "repo": p["repo"],
                    "age_days": age(p),
                    "severity": next(s for s in SEVERITY if s in label_names(p)),
                }
                for p in sorted(high, key=age, reverse=True)[:10]
            ],
        },
        "open_p0_issues": [
            {
                "number": i["number"],
                "title": i["title"],
                "repo": i["repo"],
                "age_days": age(i),
                "owner": ", ".join(x["login"] for x in i["assignees"]) or None,
            }
            for i in sorted(open_p0, key=age, reverse=True)
        ],
    }
    return report


def write_json(path: Path, data: dict) -> None:
    """Write through a temporary file and rename, so an interrupted run never leaves half a report."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Weekly PR and issue pulse: one <product>-pulse.json per product.",
        epilog=(
            "The products file maps each product name to its repos, for example:\n"
            '  {"appevolve": ["ITP-INC/appevolve-backend", "ITP-INC/appevolve-frontend"],\n'
            '   "prodtwin": ["ITP-INC/prodtwin-stable"]}\n'
            "Setup and notes are at the top of this script."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--start", required=True, help="first day, YYYY-MM-DD (UTC)")
    ap.add_argument(
        "--end", required=True, help="last day, YYYY-MM-DD (UTC), inclusive"
    )
    ap.add_argument(
        "--products",
        required=True,
        help="JSON file mapping each product name to a list of OWNER/REPO",
    )
    ap.add_argument(
        "--only",
        action="append",
        metavar="PRODUCT",
        help="report only this product (repeatable); default: every product in the file",
    )
    ap.add_argument(
        "--out-dir",
        default=".",
        help="folder for the <product>-pulse.json files (default: current folder)",
    )
    ap.add_argument(
        "--lead",
        default="Prakash-Patil",
        help='login reported separately from the team; "" means everyone is team',
    )
    ap.add_argument(
        "--briefs-s3",
        default=BRIEFS_S3,
        help=(
            f"where {REPO}'s fleet uploads deploy briefs; only read for a product that "
            f'includes that repo; "" skips it (default: {BRIEFS_S3})'
        ),
    )
    ap.add_argument("--aws-profile", help="AWS profile that can list --briefs-s3")
    a = ap.parse_args()

    try:
        start, end = dt.date.fromisoformat(a.start), dt.date.fromisoformat(a.end)
    except ValueError:
        ap.error("--start and --end must be dates like 2026-10-25")
    if end < start:
        ap.error(f"--end {end} is before --start {start}")
    try:
        products = parse_products(
            Path(a.products).read_text(encoding="utf-8"), a.products
        )
    except (OSError, ValueError) as e:
        ap.error(str(e))
    names = list(dict.fromkeys(a.only or products))
    unknown = [n for n in names if n not in products]
    if unknown:
        ap.error(
            f"--only {', '.join(unknown)}: not in {a.products} (it has: {', '.join(products)})"
        )

    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    failed: dict[str, str] = {}
    for product in names:
        repos = products[product]
        print(f"{product}: reading {len(repos)} repo(s)...")
        try:
            report = build_report(
                product, repos, start, end, a.lead, a.briefs_s3, a.aws_profile
            )
        except GhError as e:
            failed[product] = str(e)
            print(f"{product}: FAILED, no file written: {e}")
            continue
        path = out_dir / f"{product}-pulse.json"
        write_json(path, report)
        v = report["volume"]
        print(
            f"wrote {path}: {v['merged']} merged, {v['opened']} opened, "
            f"{v['deploys']} deploys"
        )
    if failed:
        raise SystemExit(
            f"{len(failed)} of {len(names)} product(s) failed: {', '.join(failed)}"
        )


if __name__ == "__main__":
    main()
