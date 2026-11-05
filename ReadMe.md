# Weekly pulse

Two read-only reporting tools. Neither changes anything in the system it reads.

| Tool | Reads | Writes | Needs |
| --- | --- | --- | --- |
| [`weekly_pr_pulse.py`](weekly_pr_pulse.py) | GitHub pull requests and issues, through `gh` | one `<product>-pulse.json` per product | Python 3.10+, the GitHub CLI |
| [`ado_readonly_export.py`](ado_readonly_export.py) | Azure DevOps work items, repos, pull requests, builds | a CSV and a JSON file per kind of item | Python 3.10+, `requests`, an Azure DevOps PAT |

## Python environment (once)

The project uses [uv](https://docs.astral.sh/uv/). From this folder:

```bash
uv sync                      # creates .venv with requests (and pytest for the tests)
uv run python ado_readonly_export.py --list-projects
uv run pytest                # the offline tests
```

`weekly_pr_pulse.py` is standard library only and also runs with a plain `python`. Only the
Azure DevOps export needs `requests`; without uv, `pip install requests` is enough.

---

## Weekly PR pulse (GitHub)

`weekly_pr_pulse.py` reads one week of pull requests and issues from GitHub and writes one
JSON report per product. A product is a name plus the repos behind it; the numbers of all
its repos are pooled into one `<product>-pulse.json`.

It is one file, Python standard library only. It only reads from GitHub; it changes nothing.

### Set up (once)

1. Install Python 3.10+ and the GitHub CLI, <https://cli.github.com>.
2. Log `gh` in with read access to every repo you will list:

   ```bash
   gh auth login --hostname intuitive-technology-partners-inc.ghe.com
   ```

   If `gh` knows more than one host, set `GH_HOST=intuitive-technology-partners-inc.ghe.com`
   so `OWNER/REPO` resolves there.

### Run

Make a products file, a JSON object of product name to list of `OWNER/REPO`:

```json
{
  "appevolve": ["ITP-INC/appevolve-backend", "ITP-INC/appevolve-frontend"],
  "prodtwin": ["ITP-INC/prodtwin-stable"]
}
```

Then run it for a week (both dates are included, UTC; Sunday to Saturday is the usual week):

```bash
uv run scripts/weekly_pr_pulse.py --products products.json --start 2026-10-25 --end 2026-10-31
```

It prints progress for each product and writes `appevolve-pulse.json` and
`prodtwin-pulse.json` to the current folder. Allow a few minutes per repo; repos are read one after another.

| Option | Default | Meaning |
| --- | --- | --- |
| `--start`, `--end` | required | First and last day, `YYYY-MM-DD` |
| `--products` | required | The products file |
| `--only NAME` | every product | Report just this product; repeat for several |
| `--out-dir` | current folder | Where the files go; created if missing |
| `--lead LOGIN` | `Prakash-Patil` | GitHub login counted apart from "team"; `""` makes everyone team |
| `--aws-profile` | none | AWS profile for the deploy lookup (below) |
| `--briefs-s3 URL` | the SemantixIQ-M deploy bucket | Deploy-brief location; `""` skips the lookup |

Product names use letters, digits, `-` and `_` (the name becomes the file name). A product
listed twice (ignoring case), an empty list or a malformed repo is refused before any
reading starts.

### What is in a report

| Key | Holds |
| --- | --- |
| `product`, `repo`, `repos` | The product, its repos, and per repo: merged, opened, closed-unmerged, open PRs, lines, link |
| `volume` | Merged, opened, closed-unmerged, deploys, lines added and deleted, lead vs team merged |
| `velocity` | Merges per day, cycle time (opened to merged) p50/p75/p90, six-week trend |
| `people` | Per author: merged, opened, open now, reviews waiting on them |
| `areas`, `hubs` | Merged PRs grouped by keyword rules on titles, with the largest PRs of each |
| `environments` | Deploys per environment |
| `readiness` | Issue counts (open, closed, opened and closed this week) for the labels `prod-readiness`, `P0`, `P1`, `sprint:stop-the-bleeding` |
| `backlog` | Open PRs by age and severity label, drafts, reviewers with the most pending requests, oldest P0/P1 PRs |
| `open_p0_issues` | Every open P0 issue with its age and owner |

Every PR and issue listed carries its `repo`, because numbers repeat across repos. Top-level
`url` is the first repo's, so build PR links from each item's `repo`.

### Things to know

- **A repo `gh` cannot read fails its product, not the run.** The product is named with the
  repo and gets no file; the other products are still written and the exit code is 1. A
  report never silently leaves a repo out. A dropped connection fails the product the same
  way: run it again with `--only <product>`.
- **The backlog is a snapshot of when you run it**, not of `--end`. The weekly numbers are
  for the dates you give.
- **Deploys exist only for `ITP-INC/SemantixIQ-M`'s fleet.** Any other product reports 0
  deploys with `volume.deploys_complete: false` and a note. For a product that includes
  that repo, the deploy count also reads S3 and needs the `aws` CLI with access
  (`--aws-profile`); without it the run still finishes and marks the count incomplete.
- **Areas and hubs are keyword rules** (the `AREAS` and `HUBS` lists in the script). They were
  written for SemantixIQ-M's work, so another product's PRs mostly land in "Other" until you
  add rules for it. The first matching area wins; a PR can match several hubs.
- Each GitHub query returns at most 2000 items. Line counts are inflated by generated files
  and merge-from-main PRs; read them as a rough signal.
- Exit codes: `0` all products written, `1` at least one failed, `2` bad command line.

---

## Azure DevOps read-only export

`ado_readonly_export.py` copies work items, repositories, pull requests and builds from
Azure DevOps for the Innovation Lab projects into CSV and JSON files.

It only reads. Every request goes through one method that allows `GET`, plus `POST` to the
two endpoints that are pure queries (WIQL and `workitemsbatch`); anything else raises before
a request is sent, and the tests check this. Still, give it a PAT with read scopes only:
**Work Items (Read), Code (Read), Build (Read), Project and Team (Read)**.

### Set up the export (once)

1. Run `uv sync` (see above), or `pip install requests`.
2. Create a file named `.env` next to the script with your org and a PAT:

   ```ini
   ADO_PAT=<personal access token>
   ADO_ORG_URL=https://dev.azure.com/<org>
   ```

   `.env` is used if there is no `.env`, and `--env PATH` overrides both. `.env` holds a
   secret; `.gitignore` already keeps it, and `ado_export/`, out of version control.

### Run the export

```bash
uv run python ado_readonly_export.py --list-projects     # every project the PAT can see
uv run python ado_readonly_export.py                      # the target projects, all four kinds of item
uv run python ado_readonly_export.py --projects "Neurostack" "DBEvolve" --entities workitems repos
uv run python ado_readonly_export.py --out-dir D:\exports\ado
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--list-projects` | off | Print the projects the PAT can see and stop |
| `--projects NAME ...` | the target projects | Export these instead; case, spaces and punctuation are ignored |
| `--entities KIND ...` | all four | Any of `workitems`, `repos`, `prs`, `builds` |
| `--out-dir DIR` | `ado_export/` next to the script | Where the files go; created if missing |
| `--env PATH` | `.env`, else `Test.env`, next to the script | The credentials file |

### Which projects

With no `--projects`, a project is exported when its name is one of these (ignoring case,
spaces and punctuation). It is an exact match, not a substring, so client projects such as
"Alter Domus - CorPro Application Modernization" are never swept in. The list is
`TARGET_PROJECTS` in the script.

| You say | ADO project | Notes |
| --- | --- | --- |
| NeuroStack | `Neurostack` | |
| IntuitiveIQ | `IntuitiveIQ` | |
| App Modernization | `Application Modernization` | Also `Appmodz`, a small separate project, is matched; see below |
| AppEvolve | `Application Modernization` | Not a project of its own: it is the `AppEvolveAI` area path (most of that project's work items) and the `AppEvolveBACKEND` and `AppEvolveUI` repos |
| DBEvolve | `DBEvolve` | |
| Apex | `APEX` | |

Snapshot from a live run on 2026-11-05, checked against independent queries (work items match
a fresh WIQL count exactly, with no duplicates and none missing):

| Project | Work items | Repos | Pull requests | Builds |
| --- | ---: | ---: | ---: | ---: |
| IntuitiveIQ | 1,572 | 1 | 0 | 0 |
| Application Modernization | 1,643 | 3 | 78 | 3 |
| Neurostack | 926 | 1 | 0 | 0 |
| DBEvolve | 560 | 1 | 0 | 0 |
| APEX | 225 | 1 | 0 | 0 |
| Appmodz | 17 | 1 | 0 | 0 |

The zeros are real, not a gap: those projects' Azure Repos are empty (size 0, no default
branch), because the code lives in GitHub. Use `weekly_pr_pulse.py` for that.

### What is written

One `<kind>.csv` and one `<kind>.json` per kind of item, all projects together, each row
carrying its `project`. The CSV is flattened for spreadsheets (UTF-8 with BOM, so Excel opens
it correctly); the JSON is the raw Azure DevOps record with `project` set to the project name.

| File | CSV columns |
| --- | --- |
| `workitems` | project, id, WorkItemType, Title, State, Reason, AssignedTo, CreatedBy, CreatedDate, ChangedDate, AreaPath, IterationPath, Tags, Priority, StoryPoints, OriginalEstimate, RemainingWork, CompletedWork, ClosedDate, Parent, url |
| `repos` | project, id, name, defaultBranch, size, isDisabled, webUrl |
| `prs` | project, id, repo, title, status, createdBy, creationDate, closedDate, sourceBranch, targetBranch, isDraft |
| `builds` | project, id, buildNumber, definition, status, result, sourceBranch, requestedFor, queueTime, finishTime |

### Things to know about the export

- **A failure is never silent.** If a project cannot be read for some kind of item (HTTP
  error, dropped connection), that project's rows for it are left out whole, never half
  written; it is named on stderr; everything else is still written; and the exit code is 1.
  If a kind of item fails for *every* project, its previous files are left untouched.
  Re-run with `--projects` and `--entities` to fill the gap.
- **A bad or expired PAT stops the run** with a message (HTTP 401/403, or the sign-in page
  Azure DevOps returns for an invalid PAT). Rate limits (429/503) and dropped connections are
  retried up to five times first.
- **Work items: 20,000 per project.** Azure DevOps refuses a query that matches more, so such
  a project is reported as skipped with Azure DevOps' own message. The largest project here
  has about 1,600.
- **Work item `url` is the API address**, not a link you can open in a browser.
- **Spreadsheet formulas.** A title that starts with `=`, `+`, `-` or `@` may be treated as a
  formula when the CSV is opened in Excel. Import the file as text if the source is untrusted.
- **Exit codes:** `0` everything written, `1` something was skipped, or a stop described
  above, `2` bad command line.

### Tests and CI

```bash
uv run pytest
```

The tests in `test_ado_readonly_export.py` replace Azure DevOps with a fake, so they need no
network and no PAT. They cover the read-only guard, `.env` parsing, project matching, retries
and error handling, paging, the CSV/JSON output and whole runs including partial failures.
`.github/workflows/ci.yml` runs them, after checking both scripts compile, on every push to
`main` and every pull request, on Python 3.10 and 3.12.
