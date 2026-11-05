#!/usr/bin/env python3
"""
Read-only Azure DevOps exporter.

Pulls work items, repositories, pull requests and builds for the Intuitive
projects (NeuroStack, IntuitiveIQ, AppEvolve, App Modernization, DBEvolve, Apex)
and writes them to CSV + JSON (one pair per entity). Files go to ado_export/ next
to this script, or to --out-dir.

Needs Python 3.10+ and `requests` (`uv sync`, or `pip install requests`).

Credentials come from .env (or Test.env) next to this script, or from --env:
    ADO_PAT=<personal access token>
    ADO_ORG_URL=https://dev.azure.com/<org>

READ-ONLY GUARANTEE
  * Every request goes through AdoReadOnlyClient._request, which only allows
    GET, plus POST to two endpoints that are pure queries (WIQL and
    workitemsbatch). Any other method/endpoint raises before a request is sent.
  * Use a PAT scoped to: Work Items (Read), Code (Read), Build (Read),
    Project and Team (Read). A read-only PAT is the real safety net.

PROJECTS
  Without --projects, a project is picked when its name (ignoring case, spaces and
  punctuation) is one of TARGET_PROJECTS. That is an exact match, not a substring, so
  client projects such as "Alter Domus - CorPro Application Modernization" are not
  swept in. AppEvolve has no project of its own: it is the AppEvolveAI area path and
  the AppEvolveBACKEND / AppEvolveUI repos inside "Application Modernization".

Exit code is 1 if any project/entity could not be read (it is named on stderr); the
rest is still written, and an entity that failed for every project keeps its
previous files.

Usage:
    python ado_readonly_export.py --list-projects
    python ado_readonly_export.py                       # the target projects
    python ado_readonly_export.py --projects "NeuroStack" "DBEvolve"
    python ado_readonly_export.py --entities workitems repos --out-dir out
"""
import argparse
import base64
import csv
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

API = "7.1"
HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "ado_export"

# Projects exported by default: normalised names (see norm()), matched exactly.
# "Application Modernization" is the project the AppEvolve work lives in.
TARGET_PROJECTS = {
    "neurostack",
    "intuitiveiq",
    "appevolve",
    "applicationmodernization",
    "appmodernization",
    "appmodz",
    "dbevolve",
    "apex",
}

# POST endpoints that only read data.
READ_ONLY_POSTS = (r"/_apis/wit/wiql$", r"/_apis/wit/workitemsbatch$")

WORKITEM_FIELDS = [
    "System.Id", "System.WorkItemType", "System.Title", "System.State",
    "System.Reason", "System.AssignedTo", "System.CreatedBy", "System.CreatedDate",
    "System.ChangedDate", "System.AreaPath", "System.IterationPath", "System.Tags",
    "Microsoft.VSTS.Common.Priority", "Microsoft.VSTS.Scheduling.StoryPoints",
    "Microsoft.VSTS.Scheduling.OriginalEstimate", "Microsoft.VSTS.Scheduling.RemainingWork",
    "Microsoft.VSTS.Scheduling.CompletedWork", "Microsoft.VSTS.Common.ClosedDate",
    "System.Parent",
]


def default_env_path() -> Path:
    """`.env` next to the script, else the older `Test.env`."""
    for name in (".env", "Test.env"):
        if (HERE / name).exists():
            return HERE / name
    return HERE / ".env"


def load_env(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"Env file not found: {path}")
    env = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    for key in ("ADO_PAT", "ADO_ORG_URL"):
        if not env.get(key):
            sys.exit(f"{key} missing in {path}")
    return env


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def select_projects(
    all_projects: list[str], requested: list[str] | None
) -> tuple[list[str], list[str]]:
    """(projects to export in org order, requested names not found in the org).

    `requested` is matched ignoring case, spaces and punctuation; None means TARGET_PROJECTS.
    """
    if requested:
        wanted = {norm(n) for n in requested}
        present = {norm(p) for p in all_projects}
        missing = [n for n in requested if norm(n) not in present]
    else:
        wanted, missing = TARGET_PROJECTS, []
    return [p for p in all_projects if norm(p) in wanted], missing


def describe_error(e: Exception) -> str:
    """One line for a failed fetch: the HTTP status and Azure DevOps' own message if it sent one."""
    resp = getattr(e, "response", None)
    if resp is None:
        return f"{type(e).__name__}: {e}"[:300]
    try:
        msg = str(resp.json().get("message", "")).strip()
    except Exception:
        msg = ""
    return f"HTTP {resp.status_code}" + (f": {msg[:250]}" if msg else "")


def retry_delay(resp, attempt: int) -> int:
    """Seconds to wait before retrying a 429/503: Retry-After if it is a number, else backoff."""
    try:
        return min(60, max(0, int(resp.headers.get("Retry-After", ""))))
    except ValueError:
        return 2**attempt


class AdoReadOnlyClient:
    def __init__(self, org_url: str, pat: str):
        self.org = org_url.rstrip("/")
        token = base64.b64encode(f":{pat}".encode()).decode()
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Basic {token}", "Accept": "application/json"})

    def _request(self, method, url, **kw):
        method = method.upper()
        if method == "POST":
            path = url.split("?")[0]
            if not any(re.search(p, path) for p in READ_ONLY_POSTS):
                raise PermissionError(f"Blocked non-read-only POST: {path}")
        elif method != "GET":
            raise PermissionError(f"Blocked non-read-only method: {method}")
        attempts = 5
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                r = self.s.request(method, url, timeout=60, **kw)
            except (requests.ConnectionError, requests.Timeout):
                if last:
                    raise
                time.sleep(2**attempt)
                continue
            if r.status_code in (429, 503) and not last:
                time.sleep(retry_delay(r, attempt))
                continue
            if r.status_code in (401, 403):
                sys.exit(f"{r.status_code} from ADO for {url.split('?')[0]} - check PAT scopes/expiry.")
            if r.status_code == 203 or "text/html" in r.headers.get("Content-Type", ""):
                # An invalid/expired PAT gets ADO's HTML sign-in page, not a 401.
                sys.exit(f"ADO returned its sign-in page for {url.split('?')[0]} - the PAT is invalid, expired or revoked.")
            r.raise_for_status()
            return r

    def get(self, url, params=None):
        p = {"api-version": API, **(params or {})}
        return self._request("GET", url, params=p)

    def post_query(self, url, body):
        return self._request("POST", url, params={"api-version": API}, json=body)

    def get_paged(self, url, params=None):
        """GET a list endpoint, following x-ms-continuationtoken."""
        params = dict(params or {})
        while True:
            r = self.get(url, params)
            yield from r.json().get("value", [])
            tok = r.headers.get("x-ms-continuationtoken")
            if not tok:
                return
            params["continuationToken"] = tok

    # ---- listings -------------------------------------------------------
    def projects(self):
        return list(self.get_paged(f"{self.org}/_apis/projects", {"$top": 200}))

    def workitems(self, project):
        wiql = {"query": (
            "SELECT [System.Id] FROM WorkItems "
            "WHERE [System.TeamProject] = @project ORDER BY [System.ChangedDate] DESC")}
        base = f"{self.org}/{quote(project)}/_apis/wit"
        ids = [w["id"] for w in self.post_query(f"{base}/wiql", wiql).json().get("workItems", [])]
        # WIQL refuses (HTTP 400, VS402337) a query matching more than 20,000 items rather
        # than truncating it, so such a project is reported as skipped, never silently cut.
        # The largest project today has ~1,600 items.
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            r = self.post_query(f"{self.org}/_apis/wit/workitemsbatch",
                                {"ids": chunk, "fields": WORKITEM_FIELDS})
            yield from r.json().get("value", [])

    def repos(self, project):
        return list(self.get_paged(f"{self.org}/{quote(project)}/_apis/git/repositories"))

    def pull_requests(self, project):
        url = f"{self.org}/{quote(project)}/_apis/git/pullrequests"
        top, skip = 100, 0
        while True:
            r = self.get(url, {"searchCriteria.status": "all", "$top": top, "$skip": skip})
            batch = r.json().get("value", [])
            yield from batch
            if len(batch) < top:
                return
            skip += top

    def builds(self, project):
        return self.get_paged(f"{self.org}/{quote(project)}/_apis/build/builds", {"$top": 200})


# ---- flattening ---------------------------------------------------------
def ident(v):
    return v.get("displayName") if isinstance(v, dict) else v


def flat_workitem(project, w):
    f = w.get("fields", {})
    row = {"project": project, "id": w["id"]}
    for k in WORKITEM_FIELDS[1:]:
        row[k.split(".")[-1]] = ident(f.get(k))
    row["url"] = f"{w.get('url', '')}"
    return row


def flat_repo(project, r):
    return {"project": project, "id": r["id"], "name": r["name"], "defaultBranch": r.get("defaultBranch"),
            "size": r.get("size"), "isDisabled": r.get("isDisabled"), "webUrl": r.get("webUrl")}


def flat_pr(project, p):
    return {"project": project, "id": p["pullRequestId"], "repo": p["repository"]["name"],
            "title": p["title"], "status": p["status"], "createdBy": ident(p.get("createdBy")),
            "creationDate": p.get("creationDate"), "closedDate": p.get("closedDate"),
            "sourceBranch": p.get("sourceRefName"), "targetBranch": p.get("targetRefName"),
            "isDraft": p.get("isDraft")}


def flat_build(project, b):
    return {"project": project, "id": b["id"], "buildNumber": b.get("buildNumber"),
            "definition": (b.get("definition") or {}).get("name"), "status": b.get("status"),
            "result": b.get("result"), "sourceBranch": b.get("sourceBranch"),
            "requestedFor": ident(b.get("requestedFor")), "queueTime": b.get("queueTime"),
            "finishTime": b.get("finishTime")}


ENTITIES = {
    "workitems": (lambda c, p: c.workitems(p), flat_workitem),
    "repos": (lambda c, p: c.repos(p), flat_repo),
    "prs": (lambda c, p: c.pull_requests(p), flat_pr),
    "builds": (lambda c, p: c.builds(p), flat_build),
}


def write(name, rows, raw, out_dir):
    """Write <name>.csv and <name>.json through temp files, so an interrupted run never leaves
    a half-written file. With no rows the CSV is removed, so it cannot go stale beside the JSON."""
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path, json_path = out_dir / f"{name}.csv", out_dir / f"{name}.json"
    if rows:
        tmp = csv_path.with_name(csv_path.name + ".tmp")
        with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        tmp.replace(csv_path)
    else:
        csv_path.unlink(missing_ok=True)
    tmp = json_path.with_name(json_path.name + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2, default=str), encoding="utf-8")
    tmp.replace(json_path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only Azure DevOps exporter")
    ap.add_argument("--env", help="path to env file (default: .env, else Test.env, next to this script)")
    ap.add_argument("--projects", nargs="*", help="project names (case/spacing ignored); default = the target projects")
    ap.add_argument("--entities", nargs="+", default=list(ENTITIES), choices=list(ENTITIES))
    ap.add_argument("--out-dir", help="folder for the CSV/JSON files (default: ado_export/ next to this script)")
    ap.add_argument("--list-projects", action="store_true", help="just list projects and exit")
    args = ap.parse_args(argv)

    env = load_env(Path(args.env) if args.env else default_env_path())
    client = AdoReadOnlyClient(env["ADO_ORG_URL"], env["ADO_PAT"])

    try:
        all_projects = [p["name"] for p in client.projects()]
    except requests.RequestException as e:
        sys.exit(f"Could not list projects: {describe_error(e)}")
    if args.list_projects:
        print("\n".join(sorted(all_projects)))
        return

    targets, missing = select_projects(all_projects, args.projects)
    if missing:
        print(f"Warning: not found in org: {missing}", file=sys.stderr)
    if not targets:
        sys.exit("No matching projects. Run with --list-projects and pass --projects explicitly.")
    print("Projects:", ", ".join(targets))

    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR
    skipped = []  # (project, entity, reason)
    for ent in args.entities:
        fetch, flatten = ENTITIES[ent]
        rows, raw, failed = [], [], 0
        for proj in targets:
            try:
                items = list(fetch(client, proj))  # all of a project's items, or none of them
            except requests.RequestException as e:
                reason = describe_error(e)
                skipped.append((proj, ent, reason))
                failed += 1
                print(f"  [{proj}] {ent}: SKIPPED ({reason})", file=sys.stderr)
                continue
            rows += [flatten(proj, i) for i in items]
            # ADO's own `project` object on repos/builds would shadow the name, so set it last.
            raw += [{**i, "project": proj} for i in items]
            print(f"  [{proj}] {ent}: {len(items)}")
        if failed == len(targets):
            print(f"-> {ent}: nothing read, previous files left untouched", file=sys.stderr)
            continue
        write(ent, rows, raw, out_dir)
        print(f"-> {out_dir / ent}.csv/.json ({len(rows)} rows)")

    if skipped:
        sys.exit(
            f"{len(skipped)} fetch(es) skipped, so the export is incomplete: "
            + "; ".join(f"{p}/{e} ({r})" for p, e, r in skipped)
        )


if __name__ == "__main__":
    main()
