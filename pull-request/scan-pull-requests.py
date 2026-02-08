#!/usr/bin/env python
# Scan open pull requests, update their statuses and print the next
# one for processing (if any).

from __future__ import print_function

import errno
import os
import pickle
import sys
from datetime import datetime

import github


class PR:
    """Wraps a PR object either from GitHub or Codeberg"""
    def __init__(self, key: str, noci: bool, head_sha: str, priority_ci: bool, updated_at: datetime):
        self.key = key
        self.noci = noci
        self.head_sha = head_sha
        self.priority_ci = priority_ci
        self.updated_at = updated_at

    def github(pr):
        key = f"github/{pr.number}"
        noci = any(x.name == "noci" for x in pr.labels)
        head_sha = pr.head.sha
        priority_ci = any(pr.name == "priority-ci" for pr in pr.labels)
        updated_at = pr.updated_at
        return PR(key, noci, head_sha, priority_ci, updated_at)

    def codeberg(pr):
        key = f"codeberg/{pr['number']}"
        noci = any(x['name'] == 'noci' for x in pr['labels'])
        head_sha = pr['head']['sha']
        priority_ci = any(x['name'] == 'priority-ci' for x in pr['labels'])
        updated_at = datetime.fromisoformat(x['updated_at'])
        return PR(key, noci, head_sha, priority_ci, updated_at)


class CommitStatus:
    def __init__(self, state):
        self.state = state

class Commit:
    """Wraps a commit object either from GitHub or Codeberg"""
    def __init__(self, sha: str, statuses: list[str], commit=None):
        self.sha = sha
        self.statuses = statuses
        self.commit = commit

    def github(commit, username):
        # skip foreign statuses
        statuses = [CommitStatus(s.state) for s in commit.get_statuses() if s.creator.login == username]
        return Commit(commit.sha, statuses, commit=commit)

    def codeberg(sha, statuses, username):
        statuses = [CommitStatus(s['status']) for s in statuses if s['creator']['login'] == username]
        return Commit(sha, statuses)


class GH:
    def __init__(self, repo, username):
        self.repo = repo
        self.username = username

    def get_pulls(self):
        for pr in self.repo.get_pulls():
            yield PR.github(pr)

    def get_commit(self, sha):
        return Commit.github(self.repo.get_commit(sha), self.username)

    def create_commit_status(self, commit, context="", state="success", description=""):
        commit.commit.create_status(
            context=context,
            state=state,
            description=description
        )


class Codeberg:
    def __init__(self, cb, username):
        self.cb = cb
        self.username = username

    def get_pulls(self):
        for pr in self.cb.pulls():
            yield PR.codeberg(pr)

    def get_commit(self, sha):
        statuses = self.cb.commit_statuses(sha)
        return Commit.codeberg(sha, statuses, self.username)

    def create_commit_status(self, commit, context="", state="success", description=""):
        self.cb.commit_set_status(
            commit.sha,
            state,
            description=description,
            context=context
        )

def scan_forge(db: dict, forge):

    to_process = []
    for pr in forge.get_pulls():
        # pr should be wrapped
        # skip PRs marked noci
        if pr.noci:
            print(f"{pr.key}: noci", file=sys.stderr)

            # if it made it to the cache, we probably need to wipe
            # pending status
            if pr.key in db:
                commit = forge.get_commit(pr.head_sha, GITHUB_USERNAME)
                for status in commit.statuses:
                    # if it's pending, mark it done
                    if status.state == "pending":
                        forge.create_commit_status(
                            commit,
                            context="gentoo-ci",
                            state="success",
                            description="Checks skipped due to [noci] label",
                        )
                    break
                del db[pr.key]

            continue

        # if it's not cached, get its status
        if pr.key not in db:
            print(f"{pr.key}: updating status ...", file=sys.stderr)
            commit = forge.get_commit(pr.head_sha)
            for status in commit.statuses:
                # if it's not pending, mark it done
                if status.state != "pending":
                    db[pr.key] = commit.sha
                    print(f"{pr.key}: at {commit.sha}", file=sys.stderr)
                else:
                    db[pr.key] = ""
                    print(f"{pr.key}: found pending", file=sys.stderr)
                break
            else:
                db[pr.key] = ""
                print(f"{pr.key}: unprocessed", file=sys.stderr)

        if db.get(pr.key, "") != pr.head_sha:
            to_process.append(pr)

    to_process = sorted(
        to_process,
        key=lambda x: (
            not x.priority_ci,
            x.updated_at,
        ),
    )
    for i, pr in enumerate(to_process):
        commit = forge.get_commit(pr.head_sha)
        if i == 0:
            desc = "QA checks in progress..."
            db[pr.key] = commit.sha
        else:
            desc = f"QA checks pending. Currently {i}. in queue."
        forge.create_commit_status(commit,
                                   context="gentoo-ci", state="pending", description=desc)

        print(
            f"{pr.key}: {db.get(pr.key, '(none)')} -> {pr.head_sha}", file=sys.stderr
        )
    return to_process


def scan_codeberg(db: dict):
    CODEBERG_USERNAME = os.environ['CODEBERG_USERNAME']
    CODEBERG_TOKEN_FILE = os.environ['CODEBERG_TOKEN_FILE']
    (owner, repo) = os.environ['CODEBERG_REPO'].split('/')
    with open(CODEBERG_TOKEN_FILE) as f:
        token = f.read().strip()

    # TODO: import new and improved version of codebergapi.py
    with CodebergAPI(owner, repo, token) as cb:
        return scan_forge(db, Codeberg(cb, CODEBERG_USERNAME))


def scan_github2(db: dict):
    """
    Given a db of knowns PRs, inspect open PRs, update commit
    statuses, and update the db accordingly. Return a list of
    outstanding PRs to process.
    """
    GITHUB_USERNAME = os.environ["GITHUB_USERNAME"]
    GITHUB_TOKEN_FILE = os.environ["GITHUB_TOKEN_FILE"]
    GITHUB_REPO = os.environ["GITHUB_REPO"]

    with open(GITHUB_TOKEN_FILE) as f:
        token = f.read().strip()

    g = github.Github(GITHUB_USERNAME, token, per_page=250)
    r = g.get_repo(GITHUB_REPO)

    return scan_forge(db, GH(r, GITHUB_USERNAME))

def scan_github(db: dict):
    """
    Given a db of knowns PRs, inspect open PRs, update commit
    statuses, and update the db accordingly. Return a list of
    outstanding PRs to process.
    """
    GITHUB_USERNAME = os.environ["GITHUB_USERNAME"]
    GITHUB_TOKEN_FILE = os.environ["GITHUB_TOKEN_FILE"]
    GITHUB_REPO = os.environ["GITHUB_REPO"]

    with open(GITHUB_TOKEN_FILE) as f:
        token = f.read().strip()

    g = github.Github(GITHUB_USERNAME, token, per_page=250)
    r = g.get_repo(GITHUB_REPO)

    to_process = []

    for pr in r.get_pulls():
        pr_key = f"github/{pr.number}"
        # skip PRs marked noci
        if any(x.name == "noci" for x in pr.labels):
            print(f"{pr_key}: noci", file=sys.stderr)

            # if it made it to the cache, we probably need to wipe
            # pending status
            if pr_key in db:
                commit = r.get_commit(pr.head.sha)
                for status in commit.get_statuses():
                    # skip foreign statuses
                    if status.creator.login != GITHUB_USERNAME:
                        continue
                    # if it's pending, mark it done
                    if status.state == "pending":
                        commit.create_status(
                            context="gentoo-ci",
                            state="success",
                            description="Checks skipped due to [noci] label",
                        )
                    break
                del db[pr_key]

            continue

        # if it's not cached, get its status
        if pr_key not in db:
            print(f"{pr_key}: updating status ...", file=sys.stderr)
            commit = r.get_commit(pr.head.sha)
            for status in commit.get_statuses():
                # skip foreign statuses
                if status.creator.login != GITHUB_USERNAME:
                    continue
                # if it's not pending, mark it done
                if status.state != "pending":
                    db[pr_key] = commit.sha
                    print(f"{pr_key}: at {commit.sha}", file=sys.stderr)
                else:
                    db[pr_key] = ""
                    print(f"{pr_key}: found pending", file=sys.stderr)
                break
            else:
                db[pr_key] = ""
                print(f"{pr_key}: unprocessed", file=sys.stderr)

        if db.get(pr_key, "") != pr.head.sha:
            to_process.append(pr)

    to_process = sorted(
        to_process,
        key=lambda x: (
            not any(x.name == "priority-ci" for x in x.labels),
            x.updated_at,
        ),
    )
    for i, pr in enumerate(to_process):
        pr_key = f"github/{pr.number}"
        commit = r.get_commit(pr.head.sha)
        if i == 0:
            desc = "QA checks in progress..."
            db[pr_key] = commit.sha
        else:
            desc = f"QA checks pending. Currently {i}. in queue."
        commit.create_status(context="gentoo-ci", state="pending", description=desc)

        print(
            f"{pr_key}: {db.get(pr.number, '(none)')} -> {pr.head.sha}", file=sys.stderr
        )

    return to_process


def main():
    PULL_REQUEST_DB = os.environ["PULL_REQUEST_DB"]

    db = {}
    try:
        with open(PULL_REQUEST_DB, "rb") as f:
            db = pickle.load(f)
    except (IOError, OSError) as e:
        if e.errno != errno.ENOENT:
            raise

    to_process = scan_github2(db)

    with open(PULL_REQUEST_DB + ".tmp", "wb") as f:
        pickle.dump(db, f)
    os.rename(PULL_REQUEST_DB + ".tmp", PULL_REQUEST_DB)

    if to_process:
        print(to_process[0].key)

    return 0


if __name__ == "__main__":
    sys.exit(main())
