"""Offline tests for ado_readonly_export.py.

No network and no PAT: HTTP is replaced by a fake, so the whole export (including the
read-only guard) runs against canned Azure DevOps responses.

    uv run pytest
"""

import base64
import csv
import json

import pytest
import requests

import ado_readonly_export as ado

ORG = "https://dev.azure.com/acme"
PAT = "s3cret-pat-value"

# Project names as they appear in the real Intuitive org (subset), including look-alikes
# that a loose keyword match would wrongly pull in.
ORG_PROJECTS = [
    "APEX",
    "Alter Domus - Application Modernisation",
    "Alter Domus - CorPro Application Modernization _ Assess Phase",
    "Application Modernization",
    "Appmodz",
    "DBEvolve",
    "Database Engineering",
    "IIQ Kenvue",
    "IntuitiveIQ",
    "Intuitive AI Accelerators",
    "Neurostack",
    "SemantixIQ",
]
EXPECTED_TARGETS = [
    "APEX",
    "Application Modernization",
    "Appmodz",
    "DBEvolve",
    "IntuitiveIQ",
    "Neurostack",
]


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = {} if body is None else body
        self.headers = headers or {"Content-Type": "application/json"}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code} error for url")
            err.response = self
            raise err


@pytest.fixture
def client(monkeypatch):
    """A real client whose HTTP layer is scripted: queue responses in `c.script`."""
    c = ado.AdoReadOnlyClient(ORG + "/", PAT)
    c.calls, c.sleeps, c.script = [], [], []

    def fake_request(method, url, **kw):
        c.calls.append((method, url, kw))
        item = c.script.pop(0) if c.script else FakeResponse()
        if isinstance(item, Exception):
            raise item
        return item

    c.s.request = fake_request
    monkeypatch.setattr(ado.time, "sleep", lambda s: c.sleeps.append(s))
    return c


# ---------------------------------------------------------------- read-only guard
@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "put", "delete"])
def test_write_methods_are_blocked_before_any_request(client, method):
    with pytest.raises(PermissionError):
        client._request(method, f"{ORG}/p/_apis/wit/workitems/1")
    assert client.calls == []


@pytest.mark.parametrize(
    "url",
    [
        f"{ORG}/p/_apis/wit/workitems/$Bug",  # would create a work item
        f"{ORG}/p/_apis/git/repositories",
        f"{ORG}/p/_apis/wit/wiql/extra",
        f"{ORG}/p/_apis/git/repositories?x=/_apis/wit/wiql",  # allow-listed text only in the query
    ],
)
def test_post_to_anything_but_the_two_query_endpoints_is_blocked(client, url):
    with pytest.raises(PermissionError):
        client._request("POST", url)
    assert client.calls == []


@pytest.mark.parametrize(
    "url", [f"{ORG}/p/_apis/wit/wiql", f"{ORG}/_apis/wit/workitemsbatch"]
)
def test_the_two_pure_query_posts_are_allowed(client, url):
    client._request("POST", url, json={})
    assert [c[0] for c in client.calls] == ["POST"]


def test_pat_is_sent_only_as_basic_auth_header_never_in_a_url(client):
    header = client.s.headers["Authorization"]
    assert base64.b64decode(header.removeprefix("Basic ")).decode() == f":{PAT}"
    client.get(f"{ORG}/_apis/projects")
    assert all(PAT not in c[1] for c in client.calls)


# ---------------------------------------------------------------------- load_env
def test_load_env_reads_values_ignores_comments_and_strips_quotes(tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        '﻿# comment\nADO_PAT = "abc"\n\nADO_ORG_URL=\'https://dev.azure.com/x\'\nJUNK\n',
        encoding="utf-8",
    )
    assert ado.load_env(f) == {
        "ADO_PAT": "abc",
        "ADO_ORG_URL": "https://dev.azure.com/x",
    }


def test_load_env_exits_when_file_missing(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        ado.load_env(tmp_path / "nope.env")


@pytest.mark.parametrize("missing", ["ADO_PAT", "ADO_ORG_URL"])
def test_load_env_exits_naming_the_missing_key(tmp_path, missing):
    f = tmp_path / ".env"
    keep = {"ADO_PAT", "ADO_ORG_URL"} - {missing}
    f.write_text("".join(f"{k}=v\n" for k in keep), encoding="utf-8")
    with pytest.raises(SystemExit, match=missing):
        ado.load_env(f)


def test_default_env_file_is_found_as_dot_env_or_test_env(tmp_path, monkeypatch):
    monkeypatch.setattr(ado, "HERE", tmp_path)
    (tmp_path / "Test.env").write_text("x", encoding="utf-8")
    assert ado.default_env_path() == tmp_path / "Test.env"
    (tmp_path / ".env").write_text("x", encoding="utf-8")
    assert ado.default_env_path() == tmp_path / ".env"  # .env wins


# -------------------------------------------------------------- project selection
def test_auto_select_picks_exactly_the_six_target_projects():
    targets, missing = ado.select_projects(ORG_PROJECTS, None)
    assert targets == EXPECTED_TARGETS and missing == []


def test_auto_select_does_not_pull_in_client_projects():
    targets, _ = ado.select_projects(ORG_PROJECTS, None)
    assert not [t for t in targets if t.startswith("Alter Domus")]


def test_explicit_projects_ignore_case_and_spacing_and_report_unknown_ones():
    targets, missing = ado.select_projects(
        ORG_PROJECTS, ["neuro stack", "DB-EVOLVE", "AppEvolve"]
    )
    assert targets == ["DBEvolve", "Neurostack"]
    assert missing == ["AppEvolve"]


# ------------------------------------------------------------ retries and errors
def test_429_is_retried_after_retry_after(client):
    client.script = [FakeResponse(429, headers={"Retry-After": "3"}), FakeResponse()]
    assert client.get(f"{ORG}/x").status_code == 200
    assert client.sleeps == [3]


def test_persistent_429_raises_and_does_not_sleep_after_the_last_attempt(client):
    client.script = [FakeResponse(429) for _ in range(5)]
    with pytest.raises(requests.HTTPError):
        client.get(f"{ORG}/x")
    assert len(client.calls) == 5
    assert len(client.sleeps) == 4


def test_non_numeric_retry_after_falls_back_to_backoff(client):
    client.script = [
        FakeResponse(503, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        FakeResponse(),
    ]
    assert client.get(f"{ORG}/x").status_code == 200
    assert client.sleeps == [1]


def test_dropped_connection_is_retried(client):
    client.script = [requests.ConnectionError("reset"), FakeResponse()]
    assert client.get(f"{ORG}/x").status_code == 200
    assert len(client.calls) == 2


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_exit_with_a_pat_hint_and_no_secrets(client, status):
    client.script = [FakeResponse(status)]
    with pytest.raises(SystemExit) as e:
        client.get(f"{ORG}/p/_apis/git/repositories", {"secret": "q"})
    msg = str(e.value)
    assert "PAT" in msg and PAT not in msg and "secret=q" not in msg


def test_azure_devops_sign_in_page_for_a_bad_pat_is_caught(client):
    """ADO answers an invalid or expired PAT with HTTP 203 and an HTML sign-in page."""
    client.script = [FakeResponse(203, body=ValueError("not json"), headers={"Content-Type": "text/html"})]
    with pytest.raises(SystemExit, match="PAT"):
        client.get(f"{ORG}/_apis/projects")


# ----------------------------------------------------------------------- paging
def test_get_paged_follows_the_continuation_token(client):
    client.script = [
        FakeResponse(body={"value": [1, 2]}, headers={"x-ms-continuationtoken": "T1"}),
        FakeResponse(body={"value": [3]}, headers={"x-ms-continuationtoken": "T2"}),
        FakeResponse(body={"value": [4]}),
    ]
    assert list(client.get_paged(f"{ORG}/x")) == [1, 2, 3, 4]
    tokens = [c[2]["params"].get("continuationToken") for c in client.calls]
    assert tokens == [None, "T1", "T2"]


@pytest.mark.parametrize("total", [0, 99, 100, 250])
def test_pull_requests_page_with_skip_until_a_short_page(client, total):
    prs = [{"pullRequestId": i} for i in range(total)]
    pages = [prs[i : i + 100] for i in range(0, total, 100)]
    if total % 100 == 0:
        pages.append([])  # a full last page forces one more (empty) request
    client.script = [FakeResponse(body={"value": p}) for p in pages]
    assert list(client.pull_requests("P")) == prs
    assert [c[2]["params"]["$skip"] for c in client.calls] == [i * 100 for i in range(len(pages))]
    assert all(c[2]["params"]["searchCriteria.status"] == "all" for c in client.calls)


def test_workitems_are_fetched_in_batches_of_200_and_project_name_is_quoted(client):
    ids = list(range(1, 451))
    client.script = [FakeResponse(body={"workItems": [{"id": i} for i in ids]})]
    for start in (0, 200, 400):
        chunk = ids[start : start + 200]
        client.script.append(
            FakeResponse(body={"value": [{"id": i, "fields": {}} for i in chunk]})
        )
    got = [w["id"] for w in client.workitems("App Modernization")]
    assert got == ids
    wiql_url = client.calls[0][1]
    assert wiql_url.endswith("/App%20Modernization/_apis/wit/wiql")
    sizes = [len(c[2]["json"]["ids"]) for c in client.calls[1:]]
    assert sizes == [200, 200, 50]
    assert client.calls[1][2]["json"]["fields"] == ado.WORKITEM_FIELDS


def test_project_with_no_workitems_makes_no_batch_call(client):
    client.script = [FakeResponse(body={"workItems": []})]
    assert list(client.workitems("P")) == []
    assert len(client.calls) == 1


def test_wiql_20000_cap_surfaces_adios_message_instead_of_truncating(client):
    """WIQL answers 400 (not a cut-off list) when a query matches more than 20,000 items."""
    client.script = [
        FakeResponse(400, body={"message": "VS402337: The number of work items returned exceeds the size limit of 20000."})
    ]
    with pytest.raises(requests.HTTPError) as e:
        list(client.workitems("Big"))
    assert "VS402337" in ado.describe_error(e.value)


# --------------------------------------------------------------------- flattening
def test_flat_workitem_handles_missing_fields_and_identity_objects():
    row = ado.flat_workitem(
        "P",
        {
            "id": 7,
            "url": "u",
            "fields": {
                "System.Title": "t",
                "System.AssignedTo": {"displayName": "Ann", "uniqueName": "a@x"},
                "System.CreatedBy": "plain string",
            },
        },
    )
    assert row["project"] == "P" and row["id"] == 7
    assert row["AssignedTo"] == "Ann" and row["CreatedBy"] == "plain string"
    assert row["StoryPoints"] is None and row["Parent"] is None
    assert len(row) == len(ado.WORKITEM_FIELDS) + 2  # id replaced by project+id, plus url


def test_flat_build_tolerates_a_missing_definition_and_requester():
    row = ado.flat_build("P", {"id": 1})
    assert row["definition"] is None and row["requestedFor"] is None


def test_flat_pr_and_repo_pick_the_documented_columns():
    pr = ado.flat_pr(
        "P",
        {
            "pullRequestId": 5,
            "repository": {"name": "r"},
            "title": "t",
            "status": "active",
            "createdBy": {"displayName": "Ann"},
        },
    )
    assert pr["id"] == 5 and pr["repo"] == "r" and pr["createdBy"] == "Ann"
    repo = ado.flat_repo("P", {"id": "g", "name": "r"})
    assert repo["defaultBranch"] is None and repo["name"] == "r"


# ---------------------------------------------------------------------- writing
def test_write_round_trips_awkward_text_through_csv_and_json(tmp_path):
    rows = [{"id": 1, "title": 'He said "hi", then\nleft', "who": "Zoë"}]
    ado.write("workitems", rows, [{"raw": 1}], tmp_path)
    with open(tmp_path / "workitems.csv", newline="", encoding="utf-8-sig") as fh:
        assert list(csv.DictReader(fh)) == [
            {"id": "1", "title": 'He said "hi", then\nleft', "who": "Zoë"}
        ]
    assert json.loads((tmp_path / "workitems.json").read_text(encoding="utf-8")) == [{"raw": 1}]


def test_write_with_no_rows_removes_a_stale_csv_so_csv_and_json_agree(tmp_path):
    (tmp_path / "prs.csv").write_text("id\n1\n", encoding="utf-8")
    ado.write("prs", [], [], tmp_path)
    assert not (tmp_path / "prs.csv").exists()
    assert json.loads((tmp_path / "prs.json").read_text(encoding="utf-8")) == []


def test_write_leaves_no_temp_files_behind(tmp_path):
    ado.write("repos", [{"id": 1}], [{"id": 1}], tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["repos.csv", "repos.json"]


# ------------------------------------------------------------ whole run, mocked HTTP
class Org:
    """Canned Azure DevOps: every project has 3 work items, 2 repos, 150 PRs and 2 builds."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or {}  # (project, entity) -> Exception or status code

    def __call__(self, session, method, url, **kw):
        self.calls.append((method.upper(), url.split("?")[0]))
        path = url.split("?")[0].removeprefix(ORG)
        parts = path.strip("/").split("/")
        project = requests.utils.unquote(parts[0]) if parts[0] != "_apis" else None
        entity = {"wiql": "workitems", "repositories": "repos", "pullrequests": "prs", "builds": "builds"}.get(parts[-1])
        if (project, entity) in self.fail:
            f = self.fail[(project, entity)]
            if isinstance(f, Exception):
                raise f
            return f if isinstance(f, FakeResponse) else FakeResponse(f)
        if path == "/_apis/projects":
            return FakeResponse(body={"value": [{"name": n} for n in ORG_PROJECTS]})
        if parts[-1] == "wiql":
            return FakeResponse(body={"workItems": [{"id": 1}, {"id": 2}, {"id": 3}]})
        if parts[-1] == "workitemsbatch":
            return FakeResponse(
                body={"value": [{"id": i, "url": f"u{i}", "fields": {"System.Title": f"t{i}"}} for i in kw["json"]["ids"]]}
            )
        # Real ADO repos and builds carry their own nested `project` object.
        nested = {"id": "guid", "name": project}
        if parts[-1] == "repositories":
            return FakeResponse(
                body={"value": [{"id": "a", "name": "a", "project": nested}, {"id": "b", "name": "b", "project": nested}]}
            )
        if parts[-1] == "pullrequests":
            skip = kw["params"]["$skip"]
            prs = [{"pullRequestId": i, "repository": {"name": "a"}, "title": "t", "status": "completed"} for i in range(150)]
            return FakeResponse(body={"value": prs[skip : skip + 100]})
        if parts[-1] == "builds":
            return FakeResponse(body={"value": [{"id": 1, "project": nested}, {"id": 2, "project": nested}]})
        raise AssertionError(f"unexpected request {method} {url}")


@pytest.fixture
def org(monkeypatch):
    o = Org()
    monkeypatch.setattr(requests.Session, "request", lambda self, m, u, **kw: o(self, m, u, **kw))
    monkeypatch.setattr(ado.time, "sleep", lambda s: None)
    return o


@pytest.fixture
def env_file(tmp_path):
    f = tmp_path / "test.env"
    f.write_text(f"ADO_PAT={PAT}\nADO_ORG_URL={ORG}\n", encoding="utf-8")
    return f


def run(env_file, out, *extra):
    return ado.main(["--env", str(env_file), "--out-dir", str(out), *extra])


def test_full_run_exports_every_entity_for_each_target_project(org, env_file, tmp_path, capsys):
    out = tmp_path / "out"
    run(env_file, out)  # no exit means success
    said = capsys.readouterr().out
    for p in EXPECTED_TARGETS:
        assert p in said
    wi = json.loads((out / "workitems.json").read_text(encoding="utf-8"))
    assert len(wi) == 3 * len(EXPECTED_TARGETS)
    assert {w["project"] for w in wi} == set(EXPECTED_TARGETS)
    with open(out / "prs.csv", newline="", encoding="utf-8-sig") as fh:
        assert len(list(csv.DictReader(fh))) == 150 * len(EXPECTED_TARGETS)
    for name in ("repos", "builds"):
        assert (out / f"{name}.csv").exists() and (out / f"{name}.json").exists()


def test_raw_json_project_is_the_project_name_for_every_entity(org, env_file, tmp_path):
    """ADO's repos/builds carry a nested `project` object; it must not replace the name."""
    out = tmp_path / "out"
    run(env_file, out)
    for ent in ("workitems", "repos", "prs", "builds"):
        rows = json.loads((out / f"{ent}.json").read_text(encoding="utf-8"))
        assert rows and {type(r["project"]) for r in rows} == {str}, ent


def test_a_full_run_never_sends_anything_but_reads(org, env_file, tmp_path):
    run(env_file, tmp_path / "out")
    methods = {m for m, _ in org.calls}
    assert methods <= {"GET", "POST"}
    assert {u.rsplit("/", 1)[-1] for m, u in org.calls if m == "POST"} <= {"wiql", "workitemsbatch"}


def test_entities_option_limits_what_is_fetched_and_written(org, env_file, tmp_path):
    out = tmp_path / "out"
    run(env_file, out, "--projects", "Neurostack", "--entities", "repos")
    assert sorted(p.name for p in out.iterdir()) == ["repos.csv", "repos.json"]
    assert not any("wiql" in u or "pullrequests" in u for _, u in org.calls)


def test_list_projects_prints_and_writes_nothing(org, env_file, tmp_path, capsys):
    out = tmp_path / "out"
    run(env_file, out, "--list-projects")
    assert "Neurostack" in capsys.readouterr().out
    assert not out.exists()


def test_a_failing_project_is_reported_and_exit_code_is_nonzero(org, env_file, tmp_path, capsys):
    org.fail[("DBEvolve", "builds")] = 500
    with pytest.raises(SystemExit) as e:
        run(env_file, tmp_path / "out")
    assert e.value.code not in (0, None)
    err = capsys.readouterr().err
    assert "DBEvolve" in err and "builds" in err
    # everything that did succeed is still written, and nothing partial from DBEvolve
    builds = json.loads((tmp_path / "out" / "builds.json").read_text(encoding="utf-8"))
    assert {b["project"] for b in builds} == set(EXPECTED_TARGETS) - {"DBEvolve"}
    assert (tmp_path / "out" / "workitems.json").exists()


def test_the_skip_message_carries_adios_own_error_text(org, env_file, tmp_path, capsys):
    org.fail[("Neurostack", "workitems")] = FakeResponse(
        400, body={"message": "VS402337: exceeds the size limit of 20000"}
    )
    with pytest.raises(SystemExit):
        run(env_file, tmp_path / "out", "--projects", "Neurostack", "DBEvolve", "--entities", "workitems")
    assert "VS402337" in capsys.readouterr().err


def test_a_dropped_connection_skips_that_project_instead_of_crashing(org, env_file, tmp_path, capsys):
    org.fail[("Neurostack", "repos")] = requests.ConnectionError("down")
    with pytest.raises(SystemExit) as e:
        run(env_file, tmp_path / "out", "--entities", "repos")
    assert e.value.code not in (0, None)
    assert "Neurostack" in capsys.readouterr().err


def test_if_every_project_fails_an_entity_the_previous_export_is_kept(org, env_file, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "repos.json").write_text('["previous good export"]', encoding="utf-8")
    (out / "repos.csv").write_text("id\nold\n", encoding="utf-8")
    for p in EXPECTED_TARGETS:
        org.fail[(p, "repos")] = 500
    with pytest.raises(SystemExit):
        run(env_file, out, "--entities", "repos")
    assert json.loads((out / "repos.json").read_text(encoding="utf-8")) == ["previous good export"]
    assert (out / "repos.csv").exists()


def test_unknown_explicit_project_is_a_warning_when_others_match(org, env_file, tmp_path, capsys):
    run(env_file, tmp_path / "out", "--projects", "Neurostack", "AppEvolve", "--entities", "repos")
    assert "AppEvolve" in capsys.readouterr().err


def test_no_matching_projects_exits_with_a_hint(org, env_file, tmp_path):
    with pytest.raises(SystemExit, match="list-projects"):
        run(env_file, tmp_path / "out", "--projects", "Nope")
