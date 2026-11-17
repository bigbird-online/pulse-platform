"""Exact pull request merge through the registered Pulse GitHub control plane.

The adapter merges only the reviewed head commit: GitHub's merge endpoint gets
that SHA (the API form of `gh pr merge --match-head-commit`), so a moved head is
refused by GitHub as well as by the readback here. GitHub's pull request record
is the authority:

- Before calling the merge endpoint the adapter durably writes a send marker for
  that exact attempt. A merged PR is credited to the kernel only when the
  attempt being read back has its marker; otherwise (merged before the first
  attempt, or after an attempt that crashed before sending) it is the terminal
  failure `merged_outside_kernel`. Effects never read a failed merge back.
- A merged PR is success only when its head is the reviewed commit and the merge
  commit is on main. An open PR still on that commit is absence (nothing merged,
  a later attempt may retry). A moved head, a closed PR or another base is
  failure and needs a new review round.
- Before sending, every check on the reviewed commit (workflow runs, other
  apps' check runs and commit statuses) must be positively green
  (`PullRequestMerge.checks`), or the head provably needs none: no
  `pull_request` workflow on the base selects the changed files and the head
  has no check at all. Pending, missing or unreadable checks are absence
  (nothing sent, a later attempt reads again); a failed check is the failure
  `checks_failed`. A human confirmation with
  `override_checks` lets failed or missing checks through, never pending or
  unreadable ones.

Requests go through the installed `pulse-gh.py` with the installed `gh` in a
fixed environment (no caller PATH, token or host); `pulse-gh` checks the
registered account and repository and reuses the account's existing gh or Git
credential. No credential is read, stored or passed here. Every response must
name the requested repository on github.com.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import stat
import subprocess
import tempfile
import time
from typing import Callable, Iterator

import yaml  # type: ignore[import-untyped]  # Existing canonical PyYAML; ci_triggers validates the data.

from . import ci_triggers
from .model import Json, encoded

METHODS = frozenset({"merge", "squash", "rebase"})
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
REQUIRED = {"pulse_gh", "gh", "relay_paths", "repositories"}
Api = Callable[[str, list[str]], Json]


# Completed check-run conclusions that are not failures. GitHub's required checks
# treat skipped and neutral as passing; PULSE workflow jobs whose `if:` excludes
# pull requests (`deploy`, `natural-acceptance`) are reported as skipped.
CHECK_SKIP = frozenset({"skipped", "neutral"})
CHECK_PAGES = 3  # Pages of 100 per list; a longer list cannot be proven complete.
CHECK_PAGE_SIZE = 100
CHECK_NAMES = 50  # Names kept per group in the WI observation; the rest are counted.
ACTIONS_APP = "github-actions"
CALL_TIMEOUT = 120  # Seconds per GitHub request.
CONTENTS_LIMIT = 1000  # GitHub lists at most this many directory entries.
BLOB = re.compile(r"[0-9a-f]{40}")
FILE_STATUSES = frozenset(
    {"added", "removed", "modified", "renamed", "copied", "changed", "unchanged"}
)


def workflow_file(path: str) -> bool:
    """A workflow GitHub reads: a YAML file directly in `.github/workflows/`."""
    name = path.removeprefix(".github/workflows/")
    return name != path and "/" not in name and name.endswith((".yml", ".yaml"))


class GitHubUnavailable(OSError):
    pass


def paged(path: str, page: int) -> str:
    return f"{path}{'&' if '?' in path else '?'}per_page={CHECK_PAGE_SIZE}&page={page}"


def check_summary(runs: list[Json], statuses: list[Json]) -> Json:
    """Classify every check on one commit: the current run of each logical check
    (`runs`, already reduced) and the current state of each status context.

    A check run waits until completed; then success passes, skipped and neutral
    are skipped, and every other conclusion (failure, cancelled, timed_out,
    action_required, startup_failure, stale, none) fails. A commit status passes
    on success, waits on pending and fails otherwise. The state is `failed` when
    any check failed, else `pending` when any waits, else `missing` when none
    passed (no checks at all, or only skipped ones), else `passed`.
    """
    groups: dict[str, list[str]] = {"passed": [], "skipped": [], "pending": [], "failed": []}
    for run in runs:
        name, conclusion = str(run["name"]), run.get("conclusion")
        if run["status"] != "completed":
            groups["pending"].append(name)
        elif conclusion == "success":
            groups["passed"].append(name)
        elif conclusion in CHECK_SKIP:
            groups["skipped"].append(name)
        else:
            groups["failed"].append(f"{name}: {conclusion}")
    for row in statuses:
        name, value = str(row["context"]), row["state"]
        if value == "success":
            groups["passed"].append(name)
        elif value == "pending":
            groups["pending"].append(name)
        else:
            groups["failed"].append(f"{name}: {value}")
    state = (
        "failed" if groups["failed"]
        else "pending" if groups["pending"]
        else "missing" if not groups["passed"]
        else "passed"
    )
    omitted = {k: len(v) - CHECK_NAMES for k, v in groups.items() if len(v) > CHECK_NAMES}
    return {
        "state": state,
        "total": len(runs) + len(statuses),
        **{k: sorted(v)[:CHECK_NAMES] for k, v in groups.items()},
        # Names beyond the kept ones, per group; their state still decided the verdict.
        **({"omitted": omitted} if omitted else {}),
    }


def shape(value: object) -> Json:
    """Validate the optional `pull_request_gate` policy section."""
    if not isinstance(value, dict) or not REQUIRED <= value.keys() or (
        value.keys() - REQUIRED - {"merge_method"}
    ):
        raise ValueError("Pull request gate requires pulse_gh, gh, relay_paths and repositories")
    for key in ("pulse_gh", "gh", "relay_paths"):
        if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
            raise ValueError("Pull request gate paths must be absolute")
    rows = value["repositories"]
    if (
        not isinstance(rows, list)
        or not 1 <= len(rows) <= 32
        or any(not isinstance(r, str) or not REPOSITORY.fullmatch(r) for r in rows)
        or len({r.lower() for r in rows}) != len(rows)
    ):
        raise ValueError("Pull request gate repositories must be unique owner/name pairs")
    if value.get("merge_method", "merge") not in METHODS:
        raise ValueError("Unsupported pull request merge method")
    return dict(value)


def merge_markers(policy: Json) -> Path:
    """Account-owned send markers, beside the publication intentions."""
    return Path(str(policy.get("publication_root", policy["delivery_root"]))) / "pull-request-merges"


def installed(path: str, *, executable: bool) -> str:
    """The protected program file the policy names; never a caller-selected one.

    A package manager's stable link (Homebrew's /opt/homebrew/bin/gh) is followed:
    the checks apply to the file it resolves to now, so an upgrade keeps working.
    """
    if not Path(path).is_absolute():
        raise GitHubUnavailable("GitHub program path must be absolute")
    try:
        target = Path(path).resolve(strict=True)
        info = target.stat()
    except (OSError, RuntimeError) as exc:
        raise GitHubUnavailable("GitHub program unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o022
        or info.st_uid not in {0, os.getuid()}
        or executable and not os.access(target, os.X_OK)
    ):
        raise GitHubUnavailable("GitHub program is not an installed protected file")
    return str(target)


def environment(gh: str) -> dict[str, str]:
    return {
        # gh reads the account's credential store; an Agent HOME cannot.
        "HOME": pwd.getpwuid(os.getuid()).pw_dir,
        # pulse-gh selects `gh` from PATH: only the installed one is reachable.
        "PATH": str(Path(gh).parent) + ":/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "GH_HOST": "github.com",
        "GH_PROMPT_DISABLED": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


class ReadSession:
    """One `pulse-gh session`: one preflight, then read-only repository GETs.

    The same installed programs and fixed environment as a single call; pulse-gh
    admits only `api repos/<repository>/...` GETs on the session. Every answer
    must arrive within the call timeout or the session is unavailable.
    """

    def __init__(self, command: list[str], env: dict[str, str]):
        try:
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=env, cwd="/",
            )
        except OSError as exc:
            raise GitHubUnavailable("GitHub control plane unavailable") from exc
        self.buffer = b""
        self.sent = 0

    def call(self, args: list[str]) -> str:
        answer = self.call_many([args])[0]
        if isinstance(answer, GitHubUnavailable):
            raise answer
        return answer

    def call_many(self, requests: list[list[str]]) -> list[str | GitHubUnavailable]:
        """Send independent GETs together; pulse-gh answers them concurrently."""
        stdin, stdout = self.process.stdin, self.process.stdout
        assert stdin is not None and stdout is not None
        idents = list(range(self.sent, self.sent + len(requests)))
        self.sent += len(requests)
        try:
            for ident, args in zip(idents, requests):
                stdin.write((json.dumps({"id": ident, "args": ["api", *args]}) + "\n").encode())
            stdin.flush()
        except OSError as exc:
            raise GitHubUnavailable("GitHub control plane unavailable") from exc
        answers: dict[int, str | GitHubUnavailable] = {}
        deadline = time.monotonic() + CALL_TIMEOUT
        with selectors.DefaultSelector() as selector:
            selector.register(stdout.fileno(), selectors.EVENT_READ)
            while len(answers) < len(idents):
                while b"\n" not in self.buffer:
                    left = deadline - time.monotonic()
                    if left <= 0 or not selector.select(left):
                        raise GitHubUnavailable("GitHub control plane timed out")
                    chunk = os.read(stdout.fileno(), 1 << 16)
                    if not chunk:
                        raise GitHubUnavailable("GitHub control plane unavailable")
                    self.buffer += chunk
                line, _, self.buffer = self.buffer.partition(b"\n")
                try:
                    answer = json.loads(line)
                except ValueError as exc:
                    raise GitHubUnavailable("GitHub response unreadable") from exc
                if not isinstance(answer, dict) or answer.get("id") not in idents:
                    raise GitHubUnavailable("GitHub control plane answered out of turn")
                # Only the exit status is used; provider text is never retained.
                answers[answer["id"]] = (
                    str(answer.get("stdout")) if answer.get("code") == 0
                    else GitHubUnavailable("GitHub request refused")
                )
        return [answers[ident] for ident in idents]

    def close(self) -> None:
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
            self.process.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            self.process.kill()
            self.process.wait()


def git_blob(content: bytes) -> str:
    """Git's object name for file content: the cache key and its integrity check."""
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


class PullRequestMerge:
    def __init__(
        self,
        config: Json,
        python: str,
        *,
        markers: Path | None = None,
        api: Api | None = None,
        fault: Callable[[str], None] | None = None,
        cache: Path | None = None,
    ):
        self.config = shape(config)
        self.python = python
        self.markers = markers
        self.api = api or self._api
        self.fault = fault or (lambda stage: None)
        # Workflow file contents by Git blob name; a private cache, never an authority.
        self.cache = cache
        self.session: ReadSession | None = None
        self.read_pulls: dict[tuple[str, int], Json] = {}  # PRs read in this session.
        self.prefetched: dict[str, str | GitHubUnavailable] = {}
        self.sessions = api is None  # Injected test APIs keep one call per request.

    @contextlib.contextmanager
    def reading(self, repository: str) -> Iterator[None]:
        """Serve this block's GETs from one pulse-gh session (one preflight)."""
        if not self.sessions or self.session is not None:
            yield
            return
        gh = installed(self.config["gh"], executable=True)
        script = installed(self.config["pulse_gh"], executable=False)
        self.session = ReadSession(
            [self.python, "-I", "-B", script, "--transport", "local",
             "--repository", repository, "session"],
            environment(gh),
        )
        try:
            yield
        finally:
            session, self.session = self.session, None
            self.read_pulls.clear()
            self.prefetched.clear()
            session.close()

    def prefetch(self, paths: list[str]) -> None:
        """Fetch independent GETs at once; each answer serves its first later request.

        Only with a session; a later request for the same path after that one
        goes to GitHub again (the final re-read of suites and runs stays fresh).
        """
        wanted = [path for path in dict.fromkeys(paths) if path not in self.prefetched]
        if self.session is None or not wanted:
            return
        for path, answer in zip(wanted, self.session.call_many([[path] for path in wanted])):
            self.prefetched[path] = answer

    def _api(self, repository: str, args: list[str]) -> Json:
        early = self.prefetched.pop(args[0], None) if len(args) == 1 else None
        if isinstance(early, GitHubUnavailable):
            raise early
        if early is not None:
            stdout = early
        elif self.session is not None and args[:1] != ["--method"]:
            stdout = self.session.call(args)
        else:
            stdout = self._run(repository, args)
        try:
            value = json.loads(stdout)
        except ValueError as exc:
            raise GitHubUnavailable("GitHub response unreadable") from exc
        if isinstance(value, list):
            return {"items": value}  # A list endpoint (pull request commits).
        if not isinstance(value, dict):
            raise GitHubUnavailable("GitHub response unreadable")
        return value

    def _run(self, repository: str, args: list[str]) -> str:
        gh = installed(self.config["gh"], executable=True)
        script = installed(self.config["pulse_gh"], executable=False)
        try:
            result = subprocess.run(
                [self.python, "-I", "-B", script, "--transport", "local",
                 "--repository", repository, "run", "--", "api", *args],
                capture_output=True, text=True, timeout=CALL_TIMEOUT, env=environment(gh),
                stdin=subprocess.DEVNULL, cwd="/",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitHubUnavailable("GitHub control plane unavailable") from exc
        if result.returncode != 0:
            # Only the exit status is used; provider text is never retained.
            raise GitHubUnavailable("GitHub request refused")
        return result.stdout

    def pull_request(self, repo: str, number: int) -> Json:
        if self.session is not None and (repo, number) in self.read_pulls:
            return self.read_pulls[(repo, number)]  # One judgment reads the PR once.
        pr = self.api(repo, [f"repos/{repo}/pulls/{number}"])
        base_repo = pr["base"]["repo"]
        if (
            base_repo["full_name"] != repo
            or not str(base_repo["html_url"]).startswith("https://github.com/")
            or pr["number"] != number
        ):
            raise GitHubUnavailable("GitHub answered for another pull request")
        if self.session is not None:
            self.read_pulls[(repo, number)] = pr
        return pr

    def commits(self, repo: str, number: int) -> tuple[list[str], bool]:
        """The commits the PR introduces relative to its base; False if GitHub truncated."""
        shas: list[str] = []
        for page in (1, 2, 3):
            rows = self.api(repo, [f"repos/{repo}/pulls/{number}/commits?per_page=100&page={page}"])
            items = rows.get("items")
            if not isinstance(items, list):
                raise GitHubUnavailable("GitHub commit list unreadable")
            shas.extend(str(row["sha"]) for row in items)
            if len(items) < 100:
                return shas, True
        return shas, False  # GitHub lists at most 250 commits of a pull request.

    def observe(self, repo: str, number: int) -> Json | None:
        """Registration fact: open and unmerged, its head, and the commits it introduces."""
        try:
            pr = self.pull_request(repo, number)
            commits, complete = self.commits(repo, number)
            return {
                "repository": repo,
                "number": number,
                "state": pr["state"],
                "merged": pr.get("merged") is True,
                "head_sha": pr["head"]["sha"],
                "base": pr["base"]["ref"],
                "commits": commits,
                "commits_complete": complete,
            }
        except (GitHubUnavailable, KeyError, TypeError):
            return None

    def listing(
        self, repo: str, path: str, key: str, page_check: Callable[[Json], bool] | None = None
    ) -> list[Json]:
        """One complete, stable GitHub list, or GitHubUnavailable.

        Every page must report the same total and every row a new integer id; the
        rows read must equal that total. A list that moves while it is paged
        (duplicates, a changed total) or exceeds the page limit proves nothing.
        """
        rows: list[Json] = []
        seen: set[int] = set()
        total: int | None = None
        for page in range(1, CHECK_PAGES + 1):
            answer = self.api(repo, [paged(path, page)])
            batch, count = answer.get(key), answer.get("total_count")
            if (
                not isinstance(batch, list)
                or type(count) is not int
                or total is not None and count != total
                or page_check is not None and not page_check(answer)
            ):
                raise GitHubUnavailable("GitHub list unreadable or changed while paging")
            total = count
            for row in batch:
                if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] in seen:
                    raise GitHubUnavailable("GitHub list changed while paging")
                seen.add(row["id"])
            rows.extend(batch)
            if len(batch) < CHECK_PAGE_SIZE or len(rows) >= total:
                break
        if len(rows) != total:
            raise GitHubUnavailable("GitHub list incomplete")
        return rows

    def changed_files(self, repo: str, number: int) -> list[str]:
        """Every path the PR changes (both names of a rename), proven complete."""
        rows = self.changed_rows(repo, number)
        paths = [str(r["filename"]) for r in rows]
        paths += [str(r["previous_filename"]) for r in rows if isinstance(r.get("previous_filename"), str)]
        return paths

    def changed_rows(self, repo: str, number: int) -> list[Json]:
        """The PR's file list as GitHub gives it, proven complete."""
        expected = self.pull_request(repo, number)["changed_files"]
        if type(expected) is not int:
            raise GitHubUnavailable("GitHub changed file count unreadable")
        rows: list[Json] = []
        for page in range(1, CHECK_PAGES + 1):
            batch = self.api(repo, [
                f"repos/{repo}/pulls/{number}/files?per_page={CHECK_PAGE_SIZE}&page={page}"
            ]).get("items")
            if not isinstance(batch, list):
                raise GitHubUnavailable("GitHub changed files unreadable")
            rows.extend(batch)
            if len(batch) < CHECK_PAGE_SIZE or len(rows) >= expected:
                break
        names = [row.get("filename") for row in rows]
        if (
            len(rows) != expected
            or not all(isinstance(n, str) and n for n in names)
            or len(set(names)) != len(names)
        ):
            raise GitHubUnavailable("GitHub changed file list incomplete or moving")
        return rows

    @staticmethod
    def pr_workflows(rows: list[Json]) -> dict[str, str | None]:
        """Workflow files the PR adds, changes (head blob) or removes (None).

        GitHub runs a `pull_request` workflow from the PR's own version, so these
        replace the base versions; a row that cannot be read exactly is unavailable.
        """
        found: dict[str, str | None] = {}
        for row in rows:
            name, status = row.get("filename"), row.get("status")
            if status not in FILE_STATUSES:
                raise GitHubUnavailable("GitHub changed file status unreadable")
            previous = row.get("previous_filename")
            if status == "renamed" and isinstance(previous, str) and workflow_file(previous):
                found[previous] = None
            if not isinstance(name, str) or not workflow_file(name):
                continue
            if status == "removed":
                found[name] = None
                continue
            sha = row.get("sha")
            if not isinstance(sha, str) or not BLOB.fullmatch(sha):
                raise GitHubUnavailable("GitHub changed workflow file unreadable")
            found[name] = sha
        return found

    def required_workflows(self, repo: str, number: int, base: str) -> tuple[set[str], Json]:
        """The `pull_request` workflows on `base` that this PR's changes must start.

        The workflow directory of the base branch is listed once (each entry names
        its Git blob and must be complete), the PR's own added, changed or removed
        workflow files replace those versions (GitHub runs the PR's version), and
        each file is read by its blob name, from the private cache when the bytes
        there hash to that name. The repository's workflow registry is not consulted: a
        workflow run names its own file. Any unreadable file or filter raises
        GitHubUnavailable, never "none required".
        """
        commit = self.base_commit(repo, base)
        blobs: dict[str, str] = {}
        for entry in self.workflow_directory(repo, commit):
            name, kind, sha = entry.get("name"), entry.get("type"), entry.get("sha")
            if (
                not isinstance(name, str) or "/" in name or not isinstance(kind, str)
                or entry.get("path") != ".github/workflows/" + name
                or not isinstance(sha, str) or not BLOB.fullmatch(sha)
            ):
                raise GitHubUnavailable("GitHub workflow directory entry unreadable")
            if kind == "file" and workflow_file(entry["path"]):
                blobs[entry["path"]] = sha
        rows = self.changed_rows(repo, number)
        files = [str(r["filename"]) for r in rows] + [
            str(r["previous_filename"]) for r in rows if isinstance(r.get("previous_filename"), str)
        ]
        from_pr = self.pr_workflows(rows)
        for path, sha_or_none in from_pr.items():
            if sha_or_none is None:
                blobs.pop(path, None)
            else:
                blobs[path] = sha_or_none
        self.prefetch([f"repos/{repo}/git/blobs/{sha}" for sha in blobs.values()
                       if self.cached_blob(sha) is None])
        required: set[str] = set()
        for path, sha in sorted(blobs.items()):
            content = self.blob(repo, sha)
            try:
                document = yaml.safe_load(content.decode())
                if ci_triggers.starts(document, files, base):
                    required.add(path)
            except (ValueError, yaml.YAMLError, UnicodeDecodeError) as exc:
                raise GitHubUnavailable("Workflow trigger cannot be read with certainty") from exc
        return required, {"base": base, "base_sha": commit, "workflows_read": sorted(blobs),
                          "workflow_blobs": dict(sorted(blobs.items())),
                          "workflows_from_pr": sorted(p for p, v in from_pr.items() if v),
                          "changed_files": len(set(files))}

    def directory(self, repo: str, path: str, commit: str) -> list[Json]:
        """A complete contents listing (a JSON list of entries, below 1000), or unavailable."""
        return self.entries(self.api(repo, [f"repos/{repo}/contents/{path}?ref={commit}"]))

    @staticmethod
    def entries(answer: Json) -> list[Json]:
        rows = answer.get("items")
        if not isinstance(rows, list) or len(rows) >= CONTENTS_LIMIT or not all(
            isinstance(e, dict) and isinstance(e.get("name"), str) and isinstance(e.get("type"), str)
            for e in rows
        ):
            raise GitHubUnavailable("GitHub directory listing unreadable or truncated")
        return rows

    def workflow_directory(self, repo: str, commit: str) -> list[Json]:
        """`.github/workflows` at `commit`. Empty only when proven absent: the root has
        no `.github`, or `.github` has no `workflows`. A refused or malformed answer
        is unavailable, never "no workflows"."""
        try:
            answer = self.api(repo, [f"repos/{repo}/contents/.github/workflows?ref={commit}"])
        except GitHubUnavailable:
            # A missing directory (404) and a refusal look alike: prove absence.
            for parent, child in (("", ".github"), (".github", "workflows")):
                found = [e for e in self.directory(repo, parent, commit) if e["name"] == child]
                if not found:
                    return []
                if found[0]["type"] != "dir":
                    raise
            raise
        return self.entries(answer)  # An answer that is not a listing is unreadable.

    def base_commit(self, repo: str, base: str) -> str:
        """The commit the base branch names now; workflows are read at exactly it."""
        answer = self.api(repo, [f"repos/{repo}/git/ref/heads/{base}"])
        target = answer.get("object") or {}
        sha = target.get("sha") if isinstance(target, dict) else None
        if answer.get("ref") != f"refs/heads/{base}" or target.get("type") != "commit" or not (
            isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha)
        ):
            raise GitHubUnavailable("GitHub base branch unreadable")
        return sha

    def blob(self, repo: str, sha: str) -> bytes:
        """File bytes named by `sha`: cached if they still hash to it, else from GitHub."""
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitHubUnavailable("GitHub blob name unreadable")
        known = self.cached_blob(sha)
        if known is not None:
            return known
        cached = self.cache / sha if self.cache is not None else None
        answer = self.api(repo, [f"repos/{repo}/git/blobs/{sha}"])
        try:
            content = base64.b64decode(str(answer["content"]))
        except (KeyError, ValueError) as exc:
            raise GitHubUnavailable("GitHub blob unreadable") from exc
        if answer.get("encoding") != "base64" or git_blob(content) != sha:
            raise GitHubUnavailable("GitHub blob does not match its name")
        if cached is not None:
            self.remember(cached, content)
        return content

    def cached_blob(self, sha: str) -> bytes | None:
        """Cached bytes for `sha` only if they still hash to it."""
        if self.cache is None or not re.fullmatch(r"[0-9a-f]{40}", sha) or (
            self.cache_root_redirects()
        ):
            return None
        path = self.cache / sha
        try:
            if path.is_symlink() or not path.is_file():
                return None
            content = path.read_bytes()
        except OSError:
            return None
        return content if git_blob(content) == sha else None

    def cache_root_redirects(self) -> bool:
        return self.cache is not None and self.cache.is_symlink()

    def remember(self, path: Path, content: bytes) -> None:
        """Best effort: a cache that cannot be written only costs a later read."""
        try:
            if self.cache_root_redirects():
                return
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".blob-", delete=False) as out:
                out.write(content)
            os.replace(out.name, path)
        except OSError:
            pass

    def run_jobs(self, repo: str, head: str, run: Json, suite: Json, prefix: str) -> list[Json]:
        """The current job of every check in a completed run's suite.

        Jobs of the run's current attempt come first. A partial re-run lists only
        the jobs it re-ran; a job name it lacks takes its newest earlier attempt.
        Together they must account for exactly the suite's check count, else the
        reading is unavailable: a suite that declares checks never passes on an
        empty job list, and a run without any check must not claim success.
        """
        count = suite.get("latest_check_runs_count")
        if type(count) is not int:
            raise GitHubUnavailable("GitHub check suite unreadable")
        jobs: list[Json] = []
        names: set[str] = set()
        for attempt in range(run["run_attempt"], 0, -1):
            if len(jobs) >= count and attempt != run["run_attempt"]:
                break
            rows = self.listing(
                repo, f"repos/{repo}/actions/runs/{run['id']}/attempts/{attempt}/jobs", "jobs"
            )
            for job in rows:
                if (
                    job.get("head_sha") != head
                    or job.get("run_id") != run["id"]
                    or job.get("run_attempt") != attempt
                    or not str(job.get("html_url", "")).lower().startswith(prefix)
                    or not isinstance(job.get("name"), str)
                ):
                    raise GitHubUnavailable("GitHub answered with jobs of another attempt")
            fresh = [job for job in rows if job["name"] not in names]
            names.update(job["name"] for job in rows)
            jobs.extend(fresh)
        if len(jobs) != count:
            raise GitHubUnavailable("A run's jobs do not account for its suite's checks")
        if not jobs and run.get("conclusion") in {"success"} | CHECK_SKIP:
            raise GitHubUnavailable("A run without any job claims success")
        return jobs

    def checks(self, repo: str, number: int, head: str, base: str) -> Json:
        """The current state of every check on `head`; GitHubUnavailable unless the
        set is proven complete and stable.

        GitHub Actions: every workflow run must have its Actions check suite. An
        Actions suite without a run is ignored once completed and pending while
        queued. A run that is not completed,
        or whose suite is not, is pending; a completed run is judged by all jobs
        of its current attempt (`run_attempt`), so a rerun replaces earlier
        attempts of that run and nothing else does: separate runs, apps and
        statuses all count. A required workflow (`required_workflows`) without a
        `pull_request` run is pending. Other apps: each suite's latest run per
        check name; an unfinished suite with check runs is pending, and a suite
        without any check run is the app's placeholder, not a check (Vercel,
        Claude and Cursor open such suites and never run them). Ignored suites
        are listed in the basis. Suites and runs are read again at the end; any
        change voids the reading. With no required workflow, no run, no status
        and only ignored suites, the head needs no checks (`not_required`).
        """
        prefix = f"https://github.com/{repo}/".lower()
        suites_path = f"repos/{repo}/commits/{head}/check-suites"
        runs_path = f"repos/{repo}/actions/runs?head_sha={head}"
        status_path = f"repos/{repo}/commits/{head}/status"
        # Independent first pages, fetched together on the session (one round trip).
        self.prefetch([
            f"repos/{repo}/git/ref/heads/{base}",
            paged(f"repos/{repo}/pulls/{number}/files", 1),
            paged(suites_path, 1), paged(runs_path, 1), paged(status_path, 1),
        ])
        required, basis = self.required_workflows(repo, number, base)

        def snapshot() -> tuple[list[Json], list[Json]]:
            suites = self.listing(repo, suites_path, "check_suites")
            runs = self.listing(repo, runs_path, "workflow_runs")
            for row in suites + runs:
                if row.get("head_sha") != head:
                    raise GitHubUnavailable("GitHub answered for another commit")
            return suites, runs

        def fingerprint(suites: list[Json], runs: list[Json]) -> object:
            return (
                sorted((s["id"], s.get("status"), s.get("conclusion"),
                        s.get("latest_check_runs_count")) for s in suites),
                sorted((r["id"], r.get("run_attempt"), r.get("status"), r.get("conclusion"))
                       for r in runs),
            )

        suites, runs = snapshot()
        actions = {s["id"]: s for s in suites if (s.get("app") or {}).get("slug") == ACTIONS_APP}
        by_suite: dict[int, Json] = {}
        for run in runs:
            if type(run.get("check_suite_id")) is not int or run["check_suite_id"] in by_suite or (
                type(run.get("run_attempt")) is not int or run["run_attempt"] < 1
            ):
                raise GitHubUnavailable("GitHub workflow run unreadable")
            by_suite[run["check_suite_id"]] = run
        if set(by_suite) - set(actions):
            raise GitHubUnavailable("A workflow run without its check suite")
        self.prefetch([
            paged(f"repos/{repo}/actions/runs/{r['id']}/attempts/{r['run_attempt']}/jobs", 1)
            for r in runs
            if r.get("status") == "completed" and actions[r["check_suite_id"]].get("status") == "completed"
        ] + [
            paged(f"repos/{repo}/check-suites/{s['id']}/check-runs", 1)
            for s in suites if s["id"] not in actions
        ])
        entries: list[Json] = []
        ignored: list[Json] = []
        for suite_id, suite in actions.items():
            paired = by_suite.get(suite_id)
            if paired is not None:
                if paired.get("status") == "completed" and suite.get("status") == "completed" and (
                    paired.get("conclusion") != suite.get("conclusion")
                ):
                    raise GitHubUnavailable("A workflow run and its check suite disagree")
                continue
            if suite.get("latest_check_runs_count") != 0:
                # Checks without their workflow run cannot be judged.
                raise GitHubUnavailable("A non-empty Actions check suite without its workflow run")
            if suite.get("status") == "completed":
                ignored.append({"app": ACTIONS_APP, "suite": suite_id, "status": "completed"})
            else:
                # Empty and queued for a workflow run GitHub has not created yet.
                entries.append({"name": f"{ACTIONS_APP} (suite {suite_id})", "status": "queued"})
        for run in runs:
            label, attempt = str(run["name"]), run["run_attempt"]
            if run.get("status") != "completed" or actions[run["check_suite_id"]].get(
                "status"
            ) != "completed":
                entries.append({"name": f"{label} (attempt {attempt})", "status": "in_progress"})
                continue
            jobs = self.run_jobs(repo, head, run, actions[run["check_suite_id"]], prefix)
            entries.extend({**job, "name": f"{label} / {job['name']}"} for job in jobs)
            clean = all(
                j.get("status") == "completed" and j.get("conclusion") in {"success"} | CHECK_SKIP
                for j in jobs
            )
            if not jobs or clean and run.get("conclusion") not in {"success"} | CHECK_SKIP:
                # The run's own result counts when no job explains it (startup
                # failure, cancellation, a run without jobs).
                entries.append({"name": f"{label} (run)", "status": "completed",
                                "conclusion": run.get("conclusion")})
        started = {r.get("path") for r in runs if r.get("event") == "pull_request"}
        entries += [{"name": f"{path} (not started)", "status": "queued"}
                    for path in sorted(required - started)]
        for suite in suites:
            app = suite.get("app") or {}
            if suite["id"] in actions:
                continue
            if type(app.get("id")) is not int:
                raise GitHubUnavailable("GitHub check suite unreadable")
            label = str(app.get("slug") or app["id"])
            count = suite.get("latest_check_runs_count")
            if type(count) is not int:
                raise GitHubUnavailable("GitHub check suite unreadable")
            checks = self.listing(
                repo, f"repos/{repo}/check-suites/{suite['id']}/check-runs", "check_runs"
            )
            if len(checks) != count:
                raise GitHubUnavailable("A check suite's count and its check runs disagree")
            if not checks:
                # Proven empty twice: the app's placeholder, not a check, whatever its status.
                ignored.append({"app": label, "suite": suite["id"], "status": suite.get("status")})
                continue
            for check in checks:
                if (
                    check.get("head_sha") != head
                    or (check.get("check_suite") or {}).get("id") != suite["id"]
                    or not str(check.get("html_url", "")).lower().startswith(prefix)
                ):
                    raise GitHubUnavailable("GitHub answered with checks of another commit")
                entries.append({**check, "name": f"{label} / {check['name']}"})
            if suite.get("status") != "completed":
                entries.append({"name": f"{label} (suite)", "status": "queued"})

        def same_commit(answer: Json) -> bool:
            return answer.get("sha") == head and (answer.get("repository") or {}).get(
                "full_name") == repo

        # The combined status already holds the latest state of each context.
        statuses = self.listing(
            repo, f"repos/{repo}/commits/{head}/status", "statuses", same_commit
        )
        if len({str(row.get("context")) for row in statuses}) != len(statuses):
            raise GitHubUnavailable("GitHub commit statuses repeat a context")
        self.prefetch([paged(suites_path, 1), paged(runs_path, 1)])  # The fresh re-read.
        if fingerprint(*snapshot()) != fingerprint(suites, runs):
            raise GitHubUnavailable("Checks changed while they were read")
        basis = {**basis, "ignored_suites": sorted(ignored, key=lambda row: str(row["suite"]))}
        if not required and not runs and not statuses and len(ignored) == len(suites):
            return {"state": "not_required", "total": 0, "basis": basis}
        return {**check_summary(entries, statuses), "basis": basis}

    def gate(self, effect: Json) -> tuple[tuple[str, Json] | None, Json]:
        """The checks' verdict before sending (None: send) and what was read."""
        target = effect["snapshot"]
        try:
            with self.reading(target["repository"]):
                summary = self.checks(
                    target["repository"], target["number"], target["head_sha"], target["base"]
                )
        except (GitHubUnavailable, KeyError, TypeError, AttributeError):
            # Nothing was sent: read again later; unreadable is never passing.
            summary = {"state": "unavailable"}
            return ("absence", {"reason": "checks_unavailable", "checks": summary}), summary
        if summary["state"] == "pending":
            return ("absence", {"reason": "checks_pending", "checks": summary}), summary
        if summary["state"] in {"passed", "not_required"}:
            # not_required: no workflow's filters select these changes, and the
            # head has no suite, run or status at all.
            return None, summary
        if target.get("override_checks") is True:
            # A human confirmed this head knowing its checks failed or are missing;
            # a running check still waits, it may yet fail.
            if summary["pending"]:
                return ("absence", {"reason": "checks_pending", "checks": summary}), summary
            return None, {**summary, "overridden": True}
        if summary["state"] == "failed":
            return ("failure", {"reason": "checks_failed", "checks": summary}), summary
        return ("absence", {"reason": "checks_missing", "checks": summary}), summary

    def marker(self, effect: Json, index: int) -> Path:
        if self.markers is None:
            raise GitHubUnavailable("Merge send markers are not configured")
        root = self.markers
        if root.is_symlink():
            raise GitHubUnavailable("Merge marker root redirects")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = root / f"{effect['effect_id']}.{index}.json"
        if path.is_symlink():
            raise GitHubUnavailable("Merge marker redirects")
        return path

    def sent(self, effect: Json, index: int) -> bool:
        return self.marker(effect, index).is_file()

    def mark(self, effect: Json, index: int) -> None:
        path = self.marker(effect, index)
        content = encoded({"effect_id": effect["effect_id"], "attempt": index,
                           "head_sha": effect["snapshot"]["head_sha"]})
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def readback(self, effect: Json, *, attempt: int | None = None) -> tuple[str, Json]:
        """Read the PR; `attempt` is the attempt index (default: the latest one)."""
        target = effect["snapshot"]
        repo, number, head = target["repository"], target["number"], target["head_sha"]
        try:
            pr = self.pull_request(repo, number)
            observed_head = pr["head"]["sha"]
            base = pr["base"]["ref"]
            facts = {"repository": repo, "number": number, "observed_head": observed_head,
                     "base": base}
            if pr.get("merged") is True:
                index = len(effect["attempts"]) - 1 if attempt is None else attempt
                if index < 0 or not self.sent(effect, index):
                    return "failure", {**facts, "reason": "merged_outside_kernel",
                                       "merged_by": (pr.get("merged_by") or {}).get("login")}
                if observed_head != head:
                    return "failure", {**facts, "reason": "merged_different_head"}
                merge = pr.get("merge_commit_sha")
                if base != target["base"] or not isinstance(merge, str):
                    return "failure", {**facts, "reason": "merged_outside_main"}
                compare = self.api(repo, [f"repos/{repo}/compare/{merge}...{base}"])
                if compare.get("status") not in {"identical", "ahead"}:
                    return "unknown", {**facts, "reason": "merge_commit_not_on_main",
                                       "merge_commit": merge}
                return "success", {
                    **facts,
                    "reason": "merged_head_observed_on_main",
                    "merged": True,
                    "merge_commit": merge,
                    "merged_at": pr.get("merged_at"),
                    "merged_by": (pr.get("merged_by") or {}).get("login"),
                }
            if pr.get("state") != "open":
                return "failure", {**facts, "reason": "pull_request_closed"}
            if base != target["base"]:
                return "failure", {**facts, "reason": "base_not_main"}
            if observed_head != head:
                return "failure", {**facts, "reason": "head_changed"}
            return "absence", {**facts, "reason": "pull_request_not_merged"}
        except (GitHubUnavailable, KeyError, TypeError):
            return "unknown", {"reason": "github_readback_unavailable"}

    def apply(self, effect: Json, content: bytes) -> tuple[str, Json]:
        if content != encoded(effect["snapshot"]):
            return "failure", {"reason": "fixed_merge_intention_mismatch"}
        # Effects applies only when no earlier attempt may have merged it; this
        # attempt's index is the number of earlier attempts.
        index = len(effect["attempts"])
        with self.reading(effect["snapshot"]["repository"]):
            outcome, details = self.readback(effect, attempt=index)
            if outcome != "absence":
                return outcome, details
            blocked, checks = self.gate(effect)
        if blocked is not None:
            return blocked[0], {**details, **blocked[1]}
        target = effect["snapshot"]
        repo = target["repository"]
        self.fault("before_merge")
        try:
            self.mark(effect, index)
        except (OSError, FileExistsError):
            return "unknown", {"reason": "merge_send_marker_unavailable"}
        try:
            self.api(repo, [
                "--method", "PUT", f"repos/{repo}/pulls/{target['number']}/merge",
                "-f", f"sha={target['head_sha']}",
                "-f", f"merge_method={self.config.get('merge_method', 'merge')}",
            ])
        except GitHubUnavailable:
            pass  # GitHub's own record decides; a refusal leaves the PR open.
        self.fault("after_merge")
        outcome, details = self.readback(effect, attempt=index)
        if outcome == "absence":
            # Refused (conflict, required checks, protection): nothing merged.
            return "absence", {**details, "reason": "merge_refused", "checks": checks}
        return outcome, {**details, "checks": checks}
