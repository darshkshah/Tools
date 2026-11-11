#!/usr/bin/env python3
"""List git branches with commits ahead/behind a target and the branch owner.

Owner is the author of the oldest commit reachable from the branch but not
from the target. Ahead and behind counts use a symmetric three-dot range.
"""

from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

PROTECTED_NAMES = frozenset({"main", "master", "develop"})
GITHUB_URLS = (
    re.compile(
        r"^https?://(?:[^@/]+@)?github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^ssh://(?:[^@/]+@)?github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
        re.IGNORECASE,
    ),
)


class GitError(Exception):
    """A git invocation failed or the repository is unusable."""


class GitHubError(Exception):
    """GitHub login or a pull-request lookup failed."""


def use_color() -> bool:
    return not os.environ.get("NO_COLOR") and sys.stdout.isatty()


def enable_windows_ansi() -> None:
    if sys.platform != "win32" or not use_color():
        return
    handle = ctypes.windll.kernel32.GetStdHandle(-11)
    mode = ctypes.c_uint()
    if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)


def paint(text: str, code: str) -> str:
    if not use_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def git(repo: str, *args: str) -> str:
    if shutil.which("git") is None:
        raise GitError("git was not found on PATH")
    try:
        result = subprocess.run(
            ["git", "--no-pager", "-C", repo, *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise GitError(f"failed to run git: {exc}") from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip() or f"git exited {result.returncode}"
        raise GitError(message)
    return result.stdout.strip()


def ensure_repo(repo: str) -> None:
    try:
        inside = git(repo, "rev-parse", "--is-inside-work-tree")
    except GitError as exc:
        raise GitError(f"{repo} is not a git repository ({exc})") from exc
    if inside != "true":
        raise GitError(f"{repo} is not a git repository")


def branch_exists(repo: str, name: str) -> bool:
    result = subprocess.run(
        ["git", "--no-pager", "-C", repo, "rev-parse", "--verify", "--quiet", name],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode == 0


def default_target(repo: str) -> str:
    try:
        ref = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    except GitError:
        ref = ""
    if ref.startswith("origin/"):
        candidate = ref[len("origin/") :]
        if branch_exists(repo, candidate):
            return candidate
    for name in ("main", "master"):
        if branch_exists(repo, name):
            return name
    raise GitError(
        "could not determine a target branch; pass --target "
        "(looked for origin's default, main, and master)"
    )


def list_branches(repo: str, include_remotes: bool) -> list[str]:
    names = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines()
    if include_remotes:
        remote_refs = git(
            repo, "for-each-ref", "--format=%(refname:short)", "refs/remotes"
        ).splitlines()
        names.extend(name for name in remote_refs if not name.endswith("/HEAD"))
    return [name for name in names if name]


def ahead_behind(repo: str, target: str, branch: str) -> tuple[int, int]:
    counts = git(repo, "rev-list", "--left-right", "--count", f"{target}...{branch}")
    left, right = counts.split()
    behind, ahead = int(left), int(right)
    return ahead, behind


def owner(repo: str, target: str, branch: str) -> str:
    # git applies -n before --reverse, so -1 would be the newest commit.
    # Take the first line of the reversed log: the oldest unique commit.
    log = git(repo, "log", f"{target}..{branch}", "--reverse", "--format=%an")
    return log.splitlines()[0] if log else "-"


def report(repo: str, target: str, branches: Iterable[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for branch in branches:
        ahead, behind = ahead_behind(repo, target, branch)
        rows.append(
            {
                "branch": branch,
                "ahead": ahead,
                "behind": behind,
                "owner": owner(repo, target, branch),
            }
        )
    return rows


def format_table(rows: list[dict[str, object]]) -> str:
    headers = ("BRANCH", "AHEAD", "BEHIND", "OWNER")
    body = [
        (str(row["branch"]), str(row["ahead"]), str(row["behind"]), str(row["owner"]))
        for row in rows
    ]
    widths = [
        max(len(headers[index]), *(len(line[index]) for line in body)) if body else len(headers[index])
        for index in range(len(headers))
    ]
    lines = [
        "  ".join(headers[index].ljust(widths[index]) for index in range(len(headers)))
    ]
    for line in body:
        lines.append(
            "  ".join(
                line[index].ljust(widths[index]) if index in (0, 3) else line[index].rjust(widths[index])
                for index in range(len(headers))
            )
        )
    return "\n".join(lines)


def parse_github_url(url: str) -> tuple[str, str] | None:
    for pattern in GITHUB_URLS:
        match = pattern.match(url.strip())
        if match:
            return match.group("owner"), match.group("repo")
    return None


def github_remote(repo: str) -> tuple[str, str, str] | None:
    found: list[tuple[str, str, str]] = []
    for name in git(repo, "remote").splitlines():
        if not name:
            continue
        parsed = parse_github_url(git(repo, "remote", "get-url", name))
        if parsed:
            found.append((name, parsed[0], parsed[1]))
    for item in found:
        if item[0] == "origin":
            return item
    return found[0] if found else None


def normalize_token(token: str) -> str:
    value = token.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not value:
        raise GitHubError("a GitHub personal access token is required")
    return value


def github_request(url: str, token: str) -> object:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "branch-report",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise GitHubError(f"GitHub API returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise GitHubError(f"could not reach GitHub ({exc.reason})") from exc
    except TimeoutError as exc:
        raise GitHubError("could not reach GitHub (timed out)") from exc
    except json.JSONDecodeError as exc:
        raise GitHubError("GitHub returned a response that was not JSON") from exc


def github_login(token: str) -> str:
    payload = github_request("https://api.github.com/user", token)
    if not isinstance(payload, dict) or not payload.get("login"):
        raise GitHubError("GitHub did not return an account name")
    return str(payload["login"])


def ensure_repo_access(owner: str, repo_name: str, token: str) -> None:
    quoted_owner = urllib.parse.quote(owner)
    quoted_repo = urllib.parse.quote(repo_name)
    github_request(f"https://api.github.com/repos/{quoted_owner}/{quoted_repo}", token)


def fetch_all_pulls(owner: str, repo_name: str, token: str) -> list[dict]:
    quoted_owner = urllib.parse.quote(owner)
    quoted_repo = urllib.parse.quote(repo_name)
    pulls: list[dict] = []
    for page in range(1, 31):
        query = urllib.parse.urlencode({"state": "all", "per_page": "100", "page": str(page)})
        url = f"https://api.github.com/repos/{quoted_owner}/{quoted_repo}/pulls?{query}"
        payload = github_request(url, token)
        if not isinstance(payload, list):
            raise GitHubError("GitHub returned an unexpected pull request list")
        batch = [item for item in payload if isinstance(item, dict)]
        pulls.extend(batch)
        if len(batch) < 100:
            break
    return pulls


class PullRequestIndex:
    """Load every pull request once, in the background, keyed by head branch."""

    def __init__(self, owner: str, repo_name: str, token: str) -> None:
        self._owner = owner
        self._repo_name = repo_name
        self._token = token
        self._cache: dict[str, str] = {}
        self._error = ""
        self._thread = threading.Thread(target=self._load, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _load(self) -> None:
        try:
            grouped: dict[str, list[dict]] = {}
            for pull in fetch_all_pulls(self._owner, self._repo_name, self._token):
                head = pull.get("head")
                if not isinstance(head, dict) or not head.get("ref"):
                    continue
                grouped.setdefault(str(head["ref"]), []).append(pull)
            self._cache = {name: summarize_pulls(items) for name, items in grouped.items()}
        except GitHubError as exc:
            self._error = str(exc)

    def summary(self, branch: str) -> str:
        self._thread.join()
        if self._error:
            return f"pull request lookup failed ({self._error})"
        return self._cache.get(branch, "no pull request")


def format_pull(pull: dict) -> str:
    user = pull.get("user") if isinstance(pull.get("user"), dict) else {}
    login = user.get("login", "unknown") if isinstance(user, dict) else "unknown"
    if pull.get("merged_at"):
        state = "merged"
    else:
        state = str(pull.get("state") or "unknown")
    title = str(pull.get("title") or "")
    url = str(pull.get("html_url") or "")
    text = f"#{pull.get('number', '?')} {state} by {login} — {title}"
    return f"{text} ({url})" if url else text


def summarize_pulls(pulls: list[dict]) -> str:
    unique: dict[object, dict] = {}
    for pull in pulls:
        unique.setdefault(pull.get("number"), pull)
    ordered = list(unique.values())
    if not ordered:
        return "no pull request"

    def group(pull: dict) -> int:
        if pull.get("state") == "open":
            return 0
        if pull.get("merged_at"):
            return 1
        return 2

    ordered.sort(key=lambda pull: str(pull.get("updated_at") or ""), reverse=True)
    ordered.sort(key=group)
    lines = [format_pull(pull) for pull in ordered[:3]]
    extra = len(ordered) - len(lines)
    if extra:
        lines.append(f"and {extra} more")
    return "\n".join(lines)


def prepare_pull_requests(repo: str) -> Callable[[str], str] | None:
    remote = github_remote(repo)
    if remote is None:
        print("No GitHub remote found. Pull request status will not be shown.")
        return None
    remote_name, owner, repo_name = remote
    print(f"GitHub pull requests will be read from {owner}/{repo_name} ({remote_name}).")
    print("Enter a personal access token with access to read pull requests.")
    print("The token is used for this run only and is not stored.")
    token = normalize_token(getpass.getpass("GitHub personal access token: "))
    login = github_login(token)
    ensure_repo_access(owner, repo_name, token)
    print(f"Logged in to GitHub as {login}.")
    index = PullRequestIndex(owner, repo_name, token)
    index.start()
    return index.summary


@dataclass
class Copy:
    label: str
    ahead: int
    behind: int
    owner: str
    tip_author: str
    tip_date: str
    other_authors: tuple[str, ...]
    commits: tuple[str, ...]
    hidden_commits: int


def checked_out_worktrees(repo: str) -> dict[str, list[str]]:
    try:
        text = git(repo, "worktree", "list", "--porcelain")
    except GitError:
        return {}
    found: dict[str, list[str]] = {}
    current: dict[str, str] = {}
    for line in text.splitlines() + [""]:
        if line == "":
            branch = current.get("branch", "")
            path = current.get("worktree", "")
            if path and branch.startswith("refs/heads/"):
                name = branch[len("refs/heads/") :]
                found.setdefault(name, []).append(path)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return found


def remote_names(repo: str) -> list[str]:
    return [name for name in git(repo, "remote").splitlines() if name]


def protection_reasons(name: str, target: str, worktrees: dict[str, list[str]]) -> list[str]:
    reasons: list[str] = []
    if name == target:
        reasons.append("target branch")
    elif name in PROTECTED_NAMES:
        reasons.append("protected branch name")
    paths = worktrees.get(name, [])
    if paths:
        reasons.append("checked out in " + ", ".join(paths))
    return reasons


FIELD = "\x1f"


@dataclass
class RefInfo:
    sha: str
    ahead: int
    behind: int
    author: str
    date: str


@dataclass
class Review:
    name: str
    reasons: tuple[str, ...]
    copies: tuple[Copy, ...]
    error: str
    pulls: str
    advice: str
    unsafe: bool


def _ahead_format(target: str) -> str:
    return (
        f"%(refname){FIELD}%(objectname){FIELD}%(ahead-behind:{target})"
        f"{FIELD}%(authorname){FIELD}%(committerdate:iso)"
    )


def _basic_format() -> str:
    return f"%(refname){FIELD}%(objectname){FIELD}%(authorname){FIELD}%(committerdate:iso)"


def _parse_ref_line(line: str, with_ahead: bool) -> tuple[str, RefInfo] | None:
    parts = line.split(FIELD)
    expected = 5 if with_ahead else 4
    if len(parts) != expected or not parts[0] or not parts[1]:
        return None
    if with_ahead:
        counts = parts[2].split()
        if len(counts) != 2:
            return None
        ahead, behind = int(counts[0]), int(counts[1])
        author, date = parts[3], parts[4]
    else:
        ahead, behind = 0, 0
        author, date = parts[2], parts[3]
    return parts[0], RefInfo(parts[1], ahead, behind, author, date)


def _unique_commits(repo: str, target: str, sha: str) -> tuple[str, tuple[str, ...], tuple[str, ...], int]:
    log = git(repo, "log", f"{target}..{sha}", "--format=%an%x09%h %s")
    rows = [line.split("\t", 1) for line in log.splitlines() if "\t" in line]
    branch_owner = rows[-1][0] if rows else "-"
    others: list[str] = []
    for author, _subject in rows:
        if author != branch_owner and author not in others:
            others.append(author)
    commits = tuple(subject for _author, subject in rows[:5])
    return branch_owner, tuple(others), commits, max(0, len(rows) - len(commits))


def _load_unique(
    repo: str, target: str, shas: set[str]
) -> dict[str, tuple[str, tuple[str, ...], tuple[str, ...], int] | GitError]:
    if not shas:
        return {}

    def job(sha: str) -> tuple[str, tuple[str, tuple[str, ...], tuple[str, ...], int] | GitError]:
        try:
            return sha, _unique_commits(repo, target, sha)
        except GitError as exc:
            return sha, exc

    loaded: dict[str, tuple[str, tuple[str, ...], tuple[str, ...], int] | GitError] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(shas))) as pool:
        for sha, result in pool.map(job, shas):
            loaded[sha] = result
    return loaded


def load_ref_metadata(repo: str, target: str) -> dict[str, RefInfo]:
    try:
        text = git(
            repo,
            "for-each-ref",
            f"--format={_ahead_format(target)}",
            "refs/heads",
            "refs/remotes",
        )
    except GitError:
        text = ""
    else:
        infos: dict[str, RefInfo] = {}
        for line in text.splitlines():
            parsed = _parse_ref_line(line, with_ahead=True)
            if parsed:
                ref, info = parsed
                infos[ref] = info
        return infos

    basic = git(repo, "for-each-ref", f"--format={_basic_format()}", "refs/heads", "refs/remotes")
    infos = {}
    for line in basic.splitlines():
        parsed = _parse_ref_line(line, with_ahead=False)
        if parsed:
            ref, info = parsed
            infos[ref] = info

    def counts(ref: str) -> tuple[str, tuple[int, int] | GitError]:
        try:
            return ref, ahead_behind(repo, target, ref)
        except GitError as exc:
            return ref, exc

    refs = list(infos)
    if not refs:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(refs))) as pool:
        for ref, result in pool.map(counts, refs):
            if isinstance(result, GitError):
                del infos[ref]
                continue
            ahead, behind = result
            info = infos[ref]
            infos[ref] = RefInfo(info.sha, ahead, behind, info.author, info.date)
    return infos


def _remote_branch(ref: str, remotes: list[str]) -> tuple[str, str] | None:
    prefix = "refs/remotes/"
    if not ref.startswith(prefix):
        return None
    rest = ref[len(prefix) :]
    matches = [remote for remote in remotes if rest.startswith(remote + "/")]
    if not matches:
        return None
    remote = max(matches, key=len)
    name = rest[len(remote) + 1 :]
    if not name or name == "HEAD":
        return None
    return remote, name


def _copy(
    info: RefInfo,
    label: str,
    unique: dict[str, tuple[str, tuple[str, ...], tuple[str, ...], int] | GitError],
) -> Copy | GitError:
    if info.ahead == 0:
        owner, others, commits, hidden = "-", (), (), 0
    else:
        loaded = unique.get(info.sha)
        if not isinstance(loaded, tuple):
            return loaded or GitError(f"could not read commits {info.sha}")
        owner, others, commits, hidden = loaded
    return Copy(
        label=label,
        ahead=info.ahead,
        behind=info.behind,
        owner=owner,
        tip_author=info.author,
        tip_date=info.date,
        other_authors=others,
        commits=commits,
        hidden_commits=hidden,
    )


def prepare_reviews(
    repo: str,
    target: str,
    *,
    pr_lookup: Callable[[str], str] | None = None,
) -> list[Review]:
    print(f"Reading branches compared with {target}...", flush=True)
    infos = load_ref_metadata(repo, target)
    remotes = remote_names(repo)
    worktrees = checked_out_worktrees(repo)
    grouped: dict[str, list[tuple[str, RefInfo]]] = {}
    for ref, info in infos.items():
        if ref.startswith("refs/heads/"):
            name = ref[len("refs/heads/") :]
            if name and name != "HEAD":
                grouped.setdefault(name, []).append(("local", info))
            continue
        split = _remote_branch(ref, remotes)
        if split:
            remote, name = split
            grouped.setdefault(name, []).append((remote, info))

    needed = {info.sha for copies in grouped.values() for _label, info in copies if info.ahead > 0}
    unique = _load_unique(repo, target, needed)
    reviews: list[Review] = []
    for name in sorted(grouped):
        reasons = tuple(protection_reasons(name, target, worktrees))
        if reasons:
            reviews.append(Review(name, reasons, (), "", "", "", False))
            continue
        ordered = sorted(grouped[name], key=lambda item: (item[0] != "local", item[0]))
        copies: list[Copy] = []
        error = ""
        for label, info in ordered:
            built = _copy(info, label, unique)
            if isinstance(built, GitError):
                error = str(built)
                break
            copies.append(built)
        if error:
            reviews.append(Review(name, (), tuple(copies), error, "", "", False))
            continue
        advice, unsafe = recommendation(copies, target)
        pulls = pr_lookup(name) if pr_lookup else ""
        reviews.append(Review(name, (), tuple(copies), "", pulls, advice, unsafe))
    return reviews


def recommendation(copies: list[Copy], target: str) -> tuple[str, bool]:
    ahead = max((copy.ahead for copy in copies), default=0)
    if ahead == 0:
        return f"Safe to delete. Every commit on this branch is already on {target}.", False
    verb = "is" if ahead == 1 else "are"
    noun = "commit" if ahead == 1 else "commits"
    shown = "that commit" if ahead == 1 else "those commits"
    return (
        f"Not safe to delete. {ahead} {noun} {verb} not on {target}. "
        f"A squash merge onto {target} does not remove {shown} from this branch.",
        True,
    )


def prompt_choice(read_answer: Callable[[str], str], prompt: str) -> str:
    while True:
        try:
            value = read_answer(prompt)
        except EOFError:
            return "q"
        if value is None:
            return "q"
        value = value.strip().lower()
        if value == "":
            value = "n"
        if value in {"y", "n", "q"}:
            return value
        print("Enter y, n, or q.")


def print_copy(copy: Copy, target: str) -> None:
    ahead = paint(str(copy.ahead), "1;33" if copy.ahead else "1;32")
    behind = paint(str(copy.behind), "1;36")
    last_author = paint(copy.tip_author, "1")
    print(
        f"  {copy.label}: {ahead} ahead, {behind} behind, "
        f"owner {copy.owner}, last commit by {last_author} on {copy.tip_date}"
    )
    if copy.other_authors:
        print(f"    other authors: {', '.join(copy.other_authors)}")
    if copy.commits:
        print(f"    commits not on {target}:")
        for commit in copy.commits:
            print(f"      {commit}")
        if copy.hidden_commits:
            extra = "commit" if copy.hidden_commits == 1 else "commits"
            print(f"      and {copy.hidden_commits} more {extra}")


def delete_copy(repo: str, name: str, copy: Copy, force: bool) -> None:
    if copy.label == "local":
        git(repo, "branch", "-D" if force else "-d", "--", name)
        print(f"  deleted local {name}")
        return
    git(repo, "push", copy.label, "--delete", f"refs/heads/{name}")
    tracking = f"refs/remotes/{copy.label}/{name}"
    if branch_exists(repo, tracking):
        git(repo, "update-ref", "-d", tracking)
    print(f"  deleted {copy.label}/{name}")


def print_summary(counts: dict[str, int]) -> None:
    print(
        "\n".join(
            [
                f"deleted: {counts['deleted']}",
                f"skipped: {counts['skipped']}",
                f"protected: {counts['protected']}",
                f"failed: {counts['failed']}",
                f"remaining: {counts['remaining']}",
            ]
        )
    )


def run_clean(
    repo: str,
    target: str,
    *,
    read_answer: Callable[[str], str],
    pr_lookup: Callable[[str], str] | None = None,
) -> int:
    try:
        git(repo, "fetch", "--all", "--prune")
    except GitError as exc:
        print(f"warning: could not fetch remotes ({exc})", file=sys.stderr)
    reviews = prepare_reviews(repo, target, pr_lookup=pr_lookup)
    counts = {"deleted": 0, "skipped": 0, "protected": 0, "failed": 0, "remaining": 0}
    enable_windows_ansi()
    print(f"Comparing branches with {target}.")
    total = len(reviews)
    try:
        for index, review in enumerate(reviews, start=1):
            name = review.name
            print("\n" + paint(f"[{index}/{total}] {name}", "1;34"))
            if review.reasons:
                reason = paint("; ".join(review.reasons), "1;33")
                print(f"  Protected: {reason}. This branch will not be deleted.")
                counts["protected"] += 1
                continue
            if review.error:
                print("  " + paint(f"Could not read this branch: {review.error}", "1;33"))
                counts["failed"] += 1
                continue
            if not review.copies:
                print("  no local or remote copy found")
                counts["skipped"] += 1
                continue
            for copy in review.copies:
                print_copy(copy, target)
            if review.pulls:
                print("  pull requests:")
                for line in review.pulls.splitlines():
                    print(f"    {line}")
            unsafe = review.unsafe
            print("  " + paint(review.advice, "1;31" if unsafe else "1;32"))
            choice = prompt_choice(
                read_answer, "Delete this branch locally and on every remote? [y/N/q] "
            )
            if choice == "q":
                counts["remaining"] += total - index + 1
                break
            if choice == "n":
                counts["skipped"] += 1
                continue
            if unsafe:
                confirm = prompt_choice(
                    read_answer,
                    f"These commits are not on {target} and will be deleted. "
                    "Delete this branch anyway? [y/N/q] ",
                )
                if confirm == "q":
                    counts["remaining"] += total - index + 1
                    break
                if confirm != "y":
                    counts["skipped"] += 1
                    continue
            errors: list[str] = []
            deleted_any = False
            for copy in review.copies:
                try:
                    delete_copy(repo, name, copy, force=unsafe)
                    deleted_any = True
                except GitError as exc:
                    errors.append(f"{copy.label}: {exc}")
                    print("  " + paint(f"Could not delete {copy.label}: {exc}", "1;33"))
            if errors:
                counts["failed"] += 1
            elif deleted_any:
                counts["deleted"] += 1
            else:
                counts["skipped"] += 1
    except KeyboardInterrupt:
        print("\nstopped")
        print_summary(counts)
        return 130
    print_summary(counts)
    return 1 if counts["failed"] else 0


HELP = """\
List every branch in a git repository and compare it with a target branch.

Columns
  BRANCH  local branch name, or remote-tracking name when --remotes is set
  AHEAD   commits on this branch that are not on the target
  BEHIND  commits on the target that are not on this branch
  OWNER   author of the oldest commit that is on this branch and not on the
          target. Shown as "-" when the branch has no commits of its own
          (the target itself, or a branch already contained in the target).

The target branch is chosen with --target. If you omit it, the tool uses
origin's default branch when that branch also exists locally, then main,
then master.

--clean reviews each branch once, including every remote, and asks before
deleting. For each branch it shows ahead, behind, owner, the last tip author
and date, any other authors of commits that are not on the target, up to five
of those commits, pull requests, and a recommendation. Those comparisons are
read from the local repository before the first prompt. Pull requests are
loaded once. After that, y, n, and q move to the next branch immediately.

The prompt is: Delete this branch locally and on every remote? [y/N/q]

  y    delete the local branch and the same branch on every remote
  n    skip this branch (Enter does this too)
  q    stop; branches already deleted stay deleted

A branch with commits that are not on the target asks for a second y before
it is deleted. main, master, develop, the target branch, and any branch
checked out in a worktree are never offered for deletion.

When a remote points at github.com, --clean asks for a personal access token.
The token is used to read pull requests for this run and is not stored.
--clean must be run from an interactive terminal.

Git must be on PATH. Errors are printed to stderr and the process exits with
status 1.

examples:
  python branch_report.py
  python branch_report.py --repo D:\\projects\\app --target develop
  python branch_report.py --remotes
  python branch_report.py --json
  python branch_report.py --clean --target main
  python branch_report.py --help
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--repo",
        default=".",
        metavar="PATH",
        help="path to the git repository (default: the current directory)",
    )
    parser.add_argument(
        "--target",
        metavar="BRANCH",
        help=(
            "branch to compare against "
            "(default: origin's default branch, then main, then master)"
        ),
    )
    parser.add_argument(
        "--remotes",
        action="store_true",
        help="also list remote-tracking branches (refs ending in /HEAD are skipped)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help='print a JSON list of objects with keys "branch", "ahead", "behind", and "owner"',
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="review every local and remote branch and delete the ones you confirm",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.clean and args.json:
        print("error: --clean cannot be combined with --json", file=sys.stderr)
        return 1
    try:
        if args.clean and not sys.stdin.isatty():
            raise GitError("--clean requires an interactive terminal")
        ensure_repo(args.repo)
        target = args.target or default_target(args.repo)
        if not branch_exists(args.repo, target):
            raise GitError(f"target branch {target!r} does not exist")
        if args.clean:
            return run_clean(
                args.repo,
                target,
                read_answer=input,
                pr_lookup=prepare_pull_requests(args.repo),
            )
        rows = report(args.repo, target, list_branches(args.repo, args.remotes))
    except (GitError, GitHubError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(format_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
