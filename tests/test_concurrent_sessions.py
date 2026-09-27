"""Several concurrent sessions on one registered project, each in its own physical workspace.

A healthy session occupying the project's working tree no longer blocks a new launch: the new session gets a
dedicated `git worktree` on its own session branch from a verified base commit, and the existing session is not
touched. Real Git repositories are used throughout. Nothing here needs the network.
"""

import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE  # noqa: E402
from test_grok_agent import GrokCase  # noqa: E402
from test_logical import TOKEN  # noqa: E402
from test_project_lock_recovery import CTX, StaleCase  # noqa: E402
from test_remote_launch import good_profile  # noqa: E402


def git(cwd, *args, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    proc = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"] + list(args),
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, stdin=subprocess.DEVNULL,
    )
    if check and proc.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), proc.stderr.decode()))
    return proc.stdout.decode().strip()


def sha(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class GitProject(object):
    """Mixin: turn the test project directory into a real Git repository with an origin/main."""

    def init_repo(self):
        repo = self.real_repo
        git(repo, "init", "-q", "-b", "main")
        with open(os.path.join(repo, "app.py"), "w") as handle:
            handle.write("print('one')\n")
        with open(os.path.join(repo, ".gitignore"), "w") as handle:
            handle.write("ignored.log\n__pycache__/\nlocal.env\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "first")
        self.origin = os.path.join(self.tmp, "origin.git")
        git(self.tmp, "init", "-q", "--bare", self.origin)
        git(repo, "remote", "add", "origin", self.origin)
        git(repo, "push", "-q", "-u", "origin", "main")
        self.base_sha = git(repo, "rev-parse", "HEAD")

    def fingerprint(self):
        """Everything about the canonical working tree an unrelated session must never change."""
        repo = self.real_repo
        tracked = git(repo, "ls-files").splitlines()
        return {
            "head": git(repo, "rev-parse", "HEAD"),
            "branch": git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False),
            "status": git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
            "index": sha(os.path.join(repo, ".git", "index")),
            "files": {name: sha(os.path.join(repo, name)) for name in tracked},
            "stash": git(repo, "stash", "list"),
            "listing": sorted(os.listdir(repo)),
        }

    def worktrees(self):
        return [ln[len("worktree "):] for ln in git(self.real_repo, "worktree", "list", "--porcelain").splitlines() if ln.startswith("worktree ")]


class ConcurrentCase(GitProject, StaleCase):
    def setUp(self):
        super().setUp()
        self.init_repo()

    def fake_terminal(self, manifest, script_file, title, test_mode):
        """Like the real launcher, Claude starts in the directory the launch script changes into."""
        with open(script_file) as handle:
            script = handle.read()
        match = re.search(r"^cd -- (\S+)", script, re.M)
        self.workdirs = getattr(self, "workdirs", []) + [match.group(1) if match else None]
        result = super().fake_terminal(manifest, script_file, title, test_mode)
        return result

    def register(self, lsid, token, session_id=None, workdir=None, env_token=None):
        if workdir is None:
            workdir = self.workdirs[-1] if getattr(self, "workdirs", None) else None
        return super().register(lsid, token, session_id=session_id or "agent-registered-%s" % lsid[-6:], workdir=workdir, env_token=env_token)

    def occupy(self, name="Nova Health"):
        """A healthy live session holding the project's main workspace."""
        return self.held(state=CORE.LS_RUNNING, pid=os.getpid(), name=name)

    def go(self, name=None, task="New work", request_id=None, project="nova", agent=None, **kw):
        self._n += 1
        body = {"project": project, "task": task, "request_id": request_id or "req-conc-%06d" % self._n}
        if name:
            body["name"] = name
        if agent:
            body["agent"] = agent
        return CORE.remote_create_session(body, CTX, terminal=self.fake_terminal, wait_seconds=0, isolation_check=self.isolation_check, **kw)

    def workspace(self, lsid):
        return CORE.logical_read(lsid)["workspace"]


class TestIsolatedLaunch(ConcurrentCase):
    def test_a_second_launch_gets_its_own_worktree_while_the_first_session_is_untouched(self):
        holder = self.occupy("Nova Health")
        before_record = copy.deepcopy(CORE.logical_read(holder))
        before_repo = self.fingerprint()
        status, view = self.go(name="PlanGuard")
        self.assertEqual(status, 202, view)
        lsid = view["logical_session_id"]
        ws = self.workspace(lsid)
        self.assertEqual(ws["kind"], "worktree")
        self.assertEqual(ws["state"], "ready")
        self.assertTrue(os.path.isdir(ws["path"]))
        self.assertIn(ws["path"], self.worktrees())  # a real, registered git worktree
        self.assertNotEqual(ws["path"], self.real_repo)
        self.assertFalse(ws["path"].startswith(self.real_repo + os.sep))  # beside the project, never inside it
        self.assertEqual(CORE.logical_read(lsid)["repository"], ws["path"])
        self.assertEqual(self.workdirs[-1], ws["path"])  # the agent is launched inside it
        # the phone is told, in plain words, and the same launch continues
        self.assertEqual(view["isolation"]["message"], "Nova Health is already using this project. A separate isolated workspace was created for PlanGuard.")
        self.assertEqual(view["isolation"]["occupied_by"]["name"], "Nova Health")
        self.assertEqual(len(self.launched), 1)
        self.assertNotIn("error", view)
        # the holder and the canonical repository are exactly as they were
        after_record = CORE.logical_read(holder)
        for key in ("state", "owner", "owner_epoch", "inbox", "history", "repository", "branch", "workspace", "stop", "approvals"):
            self.assertEqual(before_record.get(key), after_record.get(key), key)
        self.assertEqual(before_repo, self.fingerprint())

    def test_the_task_text_is_intact_through_an_isolated_launch(self):
        self.occupy()
        task = "  Plan the guard.\n\n\tKeep this ünïcode 🚀 and `ticks`.\n" + "x" * 30000
        status, view = self.go(task=task)
        self.assertEqual(status, 202, view)
        self.assertEqual(CORE.load_task(view["logical_session_id"])[0], task)

    def test_branch_and_base_sha_are_recorded_and_match_git(self):
        self.occupy()
        status, view = self.go()
        lsid = view["logical_session_id"]
        ws = self.workspace(lsid)
        self.assertEqual(ws["branch"], "th/nova/%s" % lsid[3:])
        self.assertEqual(ws["base_sha"], self.base_sha)
        self.assertEqual(ws["base_ref"], "main")
        self.assertEqual(git(ws["path"], "rev-parse", "HEAD"), self.base_sha)
        self.assertEqual(git(ws["path"], "symbolic-ref", "--short", "HEAD"), ws["branch"])
        self.assertIn(ws["branch"], git(self.real_repo, "branch", "--list", ws["branch"]))
        self.assertEqual(CORE.logical_read(lsid)["branch"], ws["branch"])
        self.assertEqual(view["isolation"]["branch"], ws["branch"])
        self.assertEqual(view["isolation"]["base_sha"], self.base_sha[:12])

    def test_the_workspace_is_private_and_owned_in_an_index(self):
        self.occupy()
        status, view = self.go()
        lsid = view["logical_session_id"]
        ws = self.workspace(lsid)
        index = json.load(open(CORE.workspaces_index_path()))["worktrees"]
        self.assertEqual(index[ws["path"]]["logical_session_id"], lsid)
        self.assertEqual(index[ws["path"]]["state"], "active")
        self.assertEqual(index[ws["path"]]["branch"], ws["branch"])
        self.assertTrue(stat.S_IMODE(os.stat(CORE.workspaces_index_path()).st_mode) == 0o600)

    def test_a_branch_other_than_the_default_starts_the_new_workspace_from_the_default_branch(self):
        git(self.real_repo, "checkout", "-q", "-b", "feature/live")
        with open(os.path.join(self.real_repo, "feature.py"), "w") as handle:
            handle.write("live work\n")
        git(self.real_repo, "add", "-A")
        git(self.real_repo, "commit", "-q", "-m", "feature")
        feature_sha = git(self.real_repo, "rev-parse", "HEAD")
        self.assertNotEqual(feature_sha, self.base_sha)
        self.occupy()
        status, view = self.go()
        ws = self.workspace(view["logical_session_id"])
        self.assertEqual((ws["base_sha"], ws["base_ref"]), (self.base_sha, "origin/main"))
        self.assertFalse(os.path.exists(os.path.join(ws["path"], "feature.py")))  # the live session's branch work is not carried over
        self.assertEqual(ws["canonical_head_branch"], "feature/live")
        self.assertEqual(ws["canonical_head_sha"], feature_sha)
        self.assertIn("starts from origin/main", " ".join(view["isolation"]["warnings"]))
        self.assertEqual(git(self.real_repo, "symbolic-ref", "--short", "HEAD"), "feature/live")  # canonical still on it

    def test_a_dirty_canonical_repo_is_surfaced_never_copied_or_disturbed(self):
        with open(os.path.join(self.real_repo, "app.py"), "a") as handle:
            handle.write("print('uncommitted edit')\n")
        with open(os.path.join(self.real_repo, "notes.txt"), "w") as handle:
            handle.write("untracked notes\n")
        git(self.real_repo, "add", "notes.txt")  # one staged file too
        with open(os.path.join(self.real_repo, "scratch.txt"), "w") as handle:
            handle.write("untracked scratch\n")
        self.occupy()
        before = self.fingerprint()
        edited = open(os.path.join(self.real_repo, "app.py")).read()
        status, view = self.go(name="PlanGuard")
        self.assertEqual(status, 202, view)
        ws = self.workspace(view["logical_session_id"])
        self.assertEqual(ws["canonical_dirty"], {"staged": 1, "modified": 1, "untracked": 1})
        warning = " ".join(view["isolation"]["warnings"])
        self.assertIn("uncommitted changes (1 modified, 1 staged, 1 untracked)", warning)
        self.assertIn("not in the new workspace", warning)
        # not copied: the workspace has the committed content only
        self.assertEqual(open(os.path.join(ws["path"], "app.py")).read(), "print('one')\n")
        self.assertFalse(os.path.exists(os.path.join(ws["path"], "notes.txt")))
        self.assertFalse(os.path.exists(os.path.join(ws["path"], "scratch.txt")))
        # not dropped: canonical is byte-identical, edits and all
        self.assertEqual(open(os.path.join(self.real_repo, "app.py")).read(), edited)
        self.assertEqual(before, self.fingerprint())
        self.assertNotIn("names", json.dumps(view["isolation"]))  # counts only, never file names
        self.assertNotIn("scratch.txt", json.dumps(view))

    def test_two_concurrent_launches_get_different_worktrees(self):
        self.occupy()
        gate = threading.Barrier(2, timeout=30)
        results = []

        def racer(n):
            gate.wait()
            results.append(self.go(name="Racer%d" % n, request_id="req-race-iso-%d" % n))

        threads = [threading.Thread(target=racer, args=(n,)) for n in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(90)
        self.assertEqual(sorted(status for status, _ in results), [202, 202], results)
        paths = [self.workspace(view["logical_session_id"])["path"] for _, view in results]
        branches = [self.workspace(view["logical_session_id"])["branch"] for _, view in results]
        self.assertEqual(len(set(paths)), 2)
        self.assertEqual(len(set(branches)), 2)
        for path in paths:
            self.assertTrue(os.path.isdir(path))
            self.assertIn(path, self.worktrees())
        self.assertEqual(len(self.launched), 2)

    def test_with_no_live_session_the_first_launch_uses_the_project_and_the_next_gets_a_worktree(self):
        first_status, first = self.go(name="First")
        self.assertEqual(first_status, 202)
        self.assertEqual(self.workspace(first["logical_session_id"])["kind"], "canonical")
        self.assertNotIn("isolation", first)
        self.assertEqual(CORE.logical_read(first["logical_session_id"])["repository"], self.real_repo)
        second_status, second = self.go(name="Second")
        self.assertEqual(second_status, 202, second)
        self.assertEqual(self.workspace(second["logical_session_id"])["kind"], "worktree")  # the first is still starting: a healthy holder
        third_status, third = self.go(name="Third")
        self.assertEqual(third_status, 202, third)
        self.assertEqual(len({self.workspace(v["logical_session_id"]).get("path") for v in (second, third)}), 2)

    def test_when_only_a_worktree_session_is_live_the_free_project_directory_is_used(self):
        canonical_holder = self.occupy("Holder")
        status, view = self.go(name="Side")
        side = view["logical_session_id"]
        CORE.logical_mutate(canonical_holder, lambda rec: rec.__setitem__("state", CORE.LS_COMPLETED))  # the canonical session ends
        CORE.logical_mutate(side, lambda rec: rec.__setitem__("state", CORE.LS_RUNNING))
        self.evidence("agent-side", os.getpid())
        CORE.logical_mutate(side, lambda rec: rec.__setitem__("owner", {"agent_session_id": "agent-side", "epoch": 1, "generation": 1}))
        status, third = self.go(name="Third")
        self.assertEqual(status, 202, third)
        self.assertEqual(self.workspace(third["logical_session_id"])["kind"], "canonical")  # the tree is free again
        self.assertNotIn("isolation", third)

    def test_the_gateway_response_carries_the_same_notice(self):
        self.occupy("Nova Health")
        status, view = self.create(name="PlanGuard", request_id="req-http-iso-0001", task="Plan it")
        self.assertIn(status, (201, 202), view)
        self.assertEqual(view["isolation"]["message"], "Nova Health is already using this project. A separate isolated workspace was created for PlanGuard.")
        self.assertEqual(view["workspace"]["isolated"], True)
        self.assertNotIn(self.tmp, json.dumps(view["workspace"]))  # never a path to the phone


class TestPermissionsAndAgents(ConcurrentCase):
    def test_project_permissions_and_the_registered_project_are_unchanged(self):
        profile = good_profile()
        profile["allow"] = list(profile["allow"]) + ["Edit(//%s/src/**)" % self.real_repo.lstrip("/")]
        profile["deny"] = list(profile.get("deny", [])) + ["Read(//%s/secrets/**)" % self.real_repo.lstrip("/")]
        CORE.project_set_permissions("nova", profile)
        CORE.project_set_remote_launch("nova", True)
        projects_before = open(os.path.join(self.home, "remote", "projects.json")).read()
        self.occupy()
        status, view = self.go()
        self.assertEqual(status, 202, view)
        self.assertEqual(open(os.path.join(self.home, "remote", "projects.json")).read(), projects_before)  # registration and profile untouched
        self.assertEqual(CORE.project_resolve("nova")[0], self.real_repo)  # the canonical project stays canonical

    def test_the_isolated_session_runs_with_the_same_profile_pointed_at_its_own_workspace(self):
        profile = good_profile()
        canonical_rule = "Edit(//%s/src/**)" % self.real_repo.lstrip("/")
        deny_rule = "Read(//%s/secrets/**)" % self.real_repo.lstrip("/")
        profile["allow"] = list(profile["allow"]) + [canonical_rule]
        profile["deny"] = list(profile.get("deny", [])) + [deny_rule]
        CORE.project_set_permissions("nova", profile)
        CORE.project_set_remote_launch("nova", True)
        self.occupy()
        status, view = self.go()
        lsid = view["logical_session_id"]
        ws = self.workspace(lsid)
        settings = json.load(open(CORE.logical_read(lsid)["permissions"]["settings_file"]))
        allow, deny = settings["permissions"]["allow"], settings["permissions"]["deny"]
        moved = "Edit(//%s/src/**)" % ws["path"].lstrip("/")
        self.assertIn(moved, allow)  # the allowance follows the session to its own workspace...
        self.assertNotIn(canonical_rule, allow)  # ...and gives no access to the tree another session occupies
        self.assertIn(deny_rule, deny)  # a deny is kept as written
        self.assertIn("Read(//%s/secrets/**)" % ws["path"].lstrip("/"), deny)  # and applied to the workspace too
        canonical_root = "//" + self.real_repo.lstrip("/")
        self.assertIn("Edit(%s/**)" % canonical_root, deny)  # an isolated agent may not edit the canonical tree
        self.assertIn("Write(%s/**)" % canonical_root, deny)
        self.assertEqual(settings["permissions"]["disableBypassPermissionsMode"], "disable")
        self.assertNotIn("bypassPermissions", json.dumps(settings))
        self.assertTrue(any("Read(" in r and "transcript" not in r and ".claude/projects" in r for r in allow))
        self.assertIn(re.sub(r"[^A-Za-z0-9]", "-", ws["path"]), json.dumps(allow))  # transcripts of the workspace, not of the project

    def test_a_canonical_session_keeps_its_rules_exactly(self):
        profile = good_profile()
        profile["allow"] = list(profile["allow"]) + ["Edit(//%s/src/**)" % self.real_repo.lstrip("/")]
        CORE.project_set_permissions("nova", profile)
        CORE.project_set_remote_launch("nova", True)
        status, view = self.go()
        settings = json.load(open(CORE.logical_read(view["logical_session_id"])["permissions"]["settings_file"]))
        self.assertIn("Edit(//%s/src/**)" % self.real_repo.lstrip("/"), settings["permissions"]["allow"])
        self.assertFalse([r for r in settings["permissions"]["deny"] if r.startswith(("Edit(//", "Write(//"))])  # no canonical-edit deny for the canonical session

    def test_claude_launch_script_and_prompt_use_the_isolated_workspace(self):
        self.occupy("Nova Health")
        status, view = self.go(name="PlanGuard")
        lsid = view["logical_session_id"]
        ws = self.workspace(lsid)
        script = self.script_texts[-1]
        self.assertIn("cd -- %s" % ws["path"], script)
        self.assertNotIn("cd -- %s\n" % self.real_repo, script)
        prompt = open(os.path.join(self.home, "prompts", "remote-%s.md" % lsid)).read()
        self.assertIn("Working directory: %s" % ws["path"], prompt)
        self.assertIn("ISOLATED Git worktree on branch %s" % ws["branch"], prompt)
        self.assertIn("never change, check out, reset or delete anything there", prompt)

    def test_claude_registers_from_its_workspace_and_only_from_it(self):
        self.occupy()
        self.behaviour = "silent"
        status, view = self.go()
        lsid = view["logical_session_id"]
        ws = self.workspace(lsid)
        token = self.token_of(self.script_texts[-1])
        self.register(lsid, token, session_id="agent-wrong-dir", workdir=self.real_repo)  # started in the canonical tree: refused
        self.assertEqual(CORE.logical_read(lsid)["state"], CORE.LS_CREATING)
        self.register(lsid, token, session_id="agent-right-dir", workdir=ws["path"])
        record = CORE.logical_read(lsid)
        self.assertEqual((record["state"], record["owner"]["agent_session_id"]), (CORE.LS_RUNNING, "agent-right-dir"))


class TestFailClosed(ConcurrentCase):
    def test_a_project_that_is_not_a_git_repository_keeps_blocking_and_says_why(self):
        import shutil
        shutil.rmtree(os.path.join(self.real_repo, ".git"))
        holder = self.occupy("Nova Health")
        status, view = self.go(name="PlanGuard")
        self.assertEqual((status, view["error"]), (409, "project_in_use"))
        self.assertIn("Nova Health", view["reason"])
        self.assertIn("not a Git repository", view["reason"])
        self.assertEqual(view["isolation"]["possible"], False)
        self.assertEqual(self.launched, [])
        self.assertEqual(CORE.logical_read(holder)["state"], CORE.LS_RUNNING)

    def test_a_project_directory_that_is_not_the_repository_top_level_is_not_isolated(self):
        parent = os.path.join(self.tmp, "monorepo")
        os.makedirs(os.path.join(parent, "sub"))
        git(parent, "init", "-q", "-b", "main")
        open(os.path.join(parent, "sub", "f.txt"), "w").write("x\n")
        git(parent, "add", "-A")
        git(parent, "commit", "-q", "-m", "m")
        CORE.project_add("mono", os.path.join(parent, "sub"))
        CORE.project_set_permissions("mono", good_profile())
        CORE.project_set_remote_launch("mono", True)
        self.other_repo = os.path.realpath(os.path.join(parent, "sub"))
        self.held(project="mono", state=CORE.LS_RUNNING, pid=os.getpid())
        status, view = self.go(project="mono")
        self.assertEqual((status, view["error"]), (409, "project_in_use"))
        self.assertIn("not the top level of its Git repository", view["reason"])
        self.assertEqual(self.launched, [])

    def test_a_repository_with_no_commits_is_not_isolated(self):
        import shutil
        shutil.rmtree(os.path.join(self.real_repo, ".git"))
        git(self.real_repo, "init", "-q", "-b", "main")
        self.occupy()
        status, view = self.go()
        self.assertEqual((status, view["error"]), (409, "project_in_use"))
        self.assertIn("no commits", view["reason"])

    def test_ambiguous_ownership_fails_closed_even_for_a_git_project(self):
        self.held(state=CORE.LS_RUNNING, pid="none")  # nothing shows it alive or dead
        before = self.worktrees()
        status, view = self.go()
        self.assertEqual((status, view["error"]), (409, "project_in_use"))
        self.assertEqual(self.worktrees(), before)
        self.assertEqual(self.launched, [])

    def test_an_orphaned_session_whose_owner_is_alive_is_ambiguous_and_blocks(self):
        self.held(state=CORE.LS_ORPHANED, pid=os.getpid())
        status, view = self.go()
        self.assertEqual((status, view["error"]), (409, "project_in_use"))
        self.assertEqual(self.launched, [])
        self.assertEqual(len(self.worktrees()), 1)

    def test_a_git_operation_in_progress_still_blocks(self):
        open(os.path.join(self.real_repo, ".git", "MERGE_HEAD"), "w").write(self.base_sha + "\n")
        self.occupy()
        status, view = self.go()
        self.assertEqual((status, view["error"]), (409, "repository_busy"))
        self.assertEqual(len(self.worktrees()), 1)

    def test_when_the_worktree_cannot_be_created_the_real_reason_is_reported_and_nothing_is_left(self):
        blocker = os.path.join(self.tmp, "not-a-directory")
        open(blocker, "w").write("a file where the workspace root should be\n")
        with open(os.path.join(self.home, "remote", "config.json"), "w") as handle:
            json.dump({"worktree_root": os.path.join(blocker, "root")}, handle)
        holder = self.occupy("Nova Health")
        status, view = self.go(name="PlanGuard")
        self.assertEqual((status, view["error"]), (503, "workspace_unavailable"))
        self.assertIn("Nova Health", view["reason"])
        self.assertIn("could not create the workspace folder", view["reason"])
        self.assertNotEqual(view["error"], "project_in_use")
        self.assertEqual(self.launched, [])
        self.assertEqual(CORE.logical_read(view["logical_session_id"])["state"], CORE.LS_FAILED)
        self.assertEqual(CORE.logical_read(holder)["state"], CORE.LS_RUNNING)
        self.assertEqual(git(self.real_repo, "branch", "--list", "th/*"), "")
        self.assertEqual(len(self.worktrees()), 1)

    def test_a_git_failure_is_reported_and_leaves_no_branch_or_directory_behind(self):
        real = CORE._git

        def failing(cwd, args, **kw):
            if args[:2] == ["worktree", "add"]:
                return 128, "", "fatal: simulated failure"
            return real(cwd, args, **kw)

        CORE._git = failing
        try:
            self.occupy()
            status, view = self.go()
        finally:
            CORE._git = real
        self.assertEqual((status, view["error"]), (503, "workspace_unavailable"))
        self.assertIn("simulated failure", view["reason"])
        self.assertEqual(git(self.real_repo, "branch", "--list", "th/*"), "")
        self.assertEqual(len(self.worktrees()), 1)

    def test_a_worktree_root_inside_the_project_is_refused(self):
        with open(os.path.join(self.home, "remote", "config.json"), "w") as handle:
            json.dump({"worktree_root": os.path.join(self.real_repo, "wt")}, handle)
        self.occupy()
        status, view = self.go()
        self.assertEqual((status, view["error"]), (409, "project_in_use"))
        self.assertIn("inside the project itself", view["reason"])

    def test_an_existing_workspace_path_is_never_reused(self):
        self.occupy()
        real_identity = CORE.workspace_identity

        def taken(project_name, plan, lsid):
            path, branch = real_identity(project_name, plan, lsid)
            os.makedirs(path)
            return path, branch

        CORE.workspace_identity = taken
        try:
            status, view = self.go()
        finally:
            CORE.workspace_identity = real_identity
        self.assertEqual((status, view["error"]), (503, "workspace_unavailable"))
        self.assertIn("refusing to reuse another session's workspace", view["reason"])


class TestStaleRecoveryStillWorks(ConcurrentCase):
    def test_a_dead_orphan_is_recovered_and_the_launch_uses_the_project_directory(self):
        old = self.held(state=CORE.LS_ORPHANED, pid=self.dead_pid, name="Brag Skill")
        status, view = self.go()
        self.assertEqual(status, 202, view)
        self.assertEqual([r["logical_session_id"] for r in view["auto_recovered"]], [old])
        self.assertEqual(CORE.logical_read(old)["state"], CORE.LS_FAILED)
        self.assertEqual(self.workspace(view["logical_session_id"])["kind"], "canonical")
        self.assertNotIn("isolation", view)
        self.assertEqual(len(self.worktrees()), 1)

    def test_a_dead_orphan_is_recovered_and_a_live_holder_is_still_left_alone(self):
        old = self.held(state=CORE.LS_ORPHANED, pid=self.dead_pid)
        live = self.occupy("Nova Health")
        before = copy.deepcopy(CORE.logical_read(live))
        status, view = self.go(name="PlanGuard")
        self.assertEqual(status, 202, view)
        self.assertEqual(CORE.logical_read(old)["state"], CORE.LS_FAILED)
        self.assertEqual(self.workspace(view["logical_session_id"])["kind"], "worktree")
        self.assertEqual(CORE.logical_read(live), before)


class TestCleanup(ConcurrentCase):
    def isolated(self):
        self.occupy()
        status, view = self.go()
        lsid = view["logical_session_id"]
        return lsid, self.workspace(lsid)

    def finish(self, lsid, state=None):
        CORE.logical_mutate(lsid, lambda rec: rec.__setitem__("state", state or CORE.LS_COMPLETED))

    def test_nothing_is_removed_while_the_session_is_not_terminal(self):
        lsid, ws = self.isolated()
        for state in (CORE.LS_CREATING, CORE.LS_RUNNING, CORE.LS_PAUSED, CORE.LS_ORPHANED, CORE.LS_STOPPED):
            CORE.logical_mutate(lsid, lambda rec, s=state: rec.__setitem__("state", s))
            self.assertEqual(CORE.cleanup_session_workspace(lsid)["state"], "not_terminal")
            self.assertTrue(os.path.isdir(ws["path"]), state)
        self.assertEqual(CORE.sweep_session_workspaces(grace=0.0, recheck=0.0), {})

    def test_a_clean_finished_workspace_is_removed_and_its_branch_and_commits_survive(self):
        lsid, ws = self.isolated()
        with open(os.path.join(ws["path"], "plan.md"), "w") as handle:
            handle.write("the plan\n")
        git(ws["path"], "add", "-A")
        git(ws["path"], "commit", "-q", "-m", "plan")
        committed = git(ws["path"], "rev-parse", "HEAD")
        self.finish(lsid)
        outcome = CORE.cleanup_session_workspace(lsid)
        self.assertEqual(outcome["state"], "removed", outcome)
        self.assertFalse(os.path.exists(ws["path"]))
        self.assertNotIn(ws["path"], self.worktrees())
        self.assertEqual(git(self.real_repo, "rev-parse", ws["branch"]), committed)  # committed work is never lost
        self.assertEqual(outcome["commits_on_branch"], 1)
        self.assertEqual(outcome["branch_retained"], ws["branch"])
        self.assertEqual(json.load(open(CORE.workspaces_index_path()))["worktrees"][ws["path"]]["state"], "removed")
        self.assertEqual(CORE.cleanup_session_workspace(lsid)["state"], "removed")  # idempotent

    def test_a_workspace_with_uncommitted_changes_is_never_deleted(self):
        lsid, ws = self.isolated()
        with open(os.path.join(ws["path"], "app.py"), "a") as handle:
            handle.write("print('precious uncommitted work')\n")
        self.finish(lsid, CORE.LS_FAILED)
        for _ in range(2):
            outcome = CORE.cleanup_session_workspace(lsid)
            self.assertEqual(outcome["state"], "kept_dirty", outcome)
            self.assertTrue(os.path.isdir(ws["path"]))
            self.assertIn("precious uncommitted work", open(os.path.join(ws["path"], "app.py")).read())
        self.assertEqual(CORE.sweep_session_workspaces(grace=0.0, recheck=0.0)[lsid]["state"], "kept_dirty")
        self.assertIn("precious uncommitted work", open(os.path.join(ws["path"], "app.py")).read())

    def test_untracked_files_staged_files_and_non_cache_ignored_files_are_never_deleted(self):
        for label, action, expected in (
            ("untracked", lambda p: open(os.path.join(p, "notes.txt"), "w").write("mine\n"), "kept_dirty"),
            ("staged", lambda p: (open(os.path.join(p, "new.py"), "w").write("x\n"), git(p, "add", "new.py")), "kept_dirty"),
            ("ignored env file", lambda p: open(os.path.join(p, "local.env"), "w").write("SECRET_TOKEN=abc\n"), "kept_ignored"),
        ):
            with self.subTest(label):
                self.tearDownProject()
                lsid, ws = self.isolated()
                action(ws["path"])
                self.finish(lsid)
                outcome = CORE.cleanup_session_workspace(lsid)
                self.assertEqual(outcome["state"], expected, outcome)
                self.assertTrue(os.path.isdir(ws["path"]))

    def tearDownProject(self):
        for record in CORE.logical_list():
            if record["state"] not in CORE.LS_TERMINAL:
                CORE.logical_mutate(record["logical_session_id"], lambda rec: rec.__setitem__("state", CORE.LS_COMPLETED))

    def test_regenerable_caches_do_not_keep_a_workspace_alive(self):
        lsid, ws = self.isolated()
        os.makedirs(os.path.join(ws["path"], "__pycache__"))
        open(os.path.join(ws["path"], "__pycache__", "x.pyc"), "wb").write(b"\0")
        self.finish(lsid)
        self.assertEqual(CORE.cleanup_session_workspace(lsid)["state"], "removed")

    def test_a_live_owner_keeps_its_workspace_even_if_the_record_says_finished(self):
        lsid, ws = self.isolated()
        self.evidence("agent-still-here", os.getpid())
        CORE.logical_mutate(lsid, lambda rec: rec.__setitem__("owner", {"agent_session_id": "agent-still-here", "epoch": 1, "generation": 1}))
        self.finish(lsid, CORE.LS_FAILED)
        outcome = CORE.cleanup_session_workspace(lsid)
        self.assertEqual(outcome["state"], "kept_owner_alive", outcome)
        self.assertTrue(os.path.isdir(ws["path"]))

    def test_a_git_operation_in_progress_keeps_the_workspace(self):
        lsid, ws = self.isolated()
        gitdir = git(ws["path"], "rev-parse", "--git-dir")
        open(os.path.join(gitdir if os.path.isabs(gitdir) else os.path.join(ws["path"], gitdir), "MERGE_HEAD"), "w").write(self.base_sha + "\n")
        self.finish(lsid)
        self.assertEqual(CORE.cleanup_session_workspace(lsid)["state"], "kept_unsafe")
        self.assertTrue(os.path.isdir(ws["path"]))

    def test_a_tampered_record_can_never_point_cleanup_at_the_canonical_project_or_anything_else(self):
        lsid, ws = self.isolated()
        self.finish(lsid)
        before = self.fingerprint()
        for bad in (self.real_repo, os.path.dirname(ws["path"]), self.tmp, "/", os.path.join(ws["path"], "..", "..")):
            with self.subTest(bad):
                CORE.logical_mutate(lsid, lambda rec, b=bad: rec["workspace"].__setitem__("path", b))
                CORE.logical_mutate(lsid, lambda rec: rec["workspace"].__setitem__("cleanup", None))
                outcome = CORE.cleanup_session_workspace(lsid)
                self.assertEqual(outcome["state"], "kept_unsafe", outcome)
                self.assertTrue(os.path.isdir(self.real_repo))
                self.assertTrue(os.path.isdir(ws["path"]))
        self.assertEqual(before, self.fingerprint())

    def test_a_workspace_not_owned_in_the_index_is_not_removed(self):
        lsid, ws = self.isolated()
        self.finish(lsid)
        CORE.update_json_locked(CORE.workspaces_index_path(), lambda d: d["worktrees"][ws["path"]].__setitem__("logical_session_id", "ls_" + "f" * 24))
        self.assertEqual(CORE.cleanup_session_workspace(lsid)["state"], "kept_unsafe")
        self.assertTrue(os.path.isdir(ws["path"]))

    def test_the_sweep_waits_out_the_grace_period_and_only_touches_finished_sessions(self):
        lsid, ws = self.isolated()
        self.finish(lsid)
        self.assertEqual(CORE.cleanup_session_workspace(lsid, grace=3600.0)["state"], "too_soon")
        self.assertTrue(os.path.isdir(ws["path"]))
        live = self.occupy("Second holder")
        swept = CORE.sweep_session_workspaces(grace=0.0, recheck=0.0)
        self.assertNotIn(live, swept)  # a live session is never a candidate
        self.assertEqual(swept[lsid]["state"], "removed")

    def test_a_canonical_session_has_nothing_to_clean_up(self):
        status, view = self.go()
        lsid = view["logical_session_id"]
        self.finish(lsid)
        self.assertEqual(CORE.cleanup_session_workspace(lsid)["state"], "not_applicable")
        self.assertTrue(os.path.isdir(self.real_repo))

    def test_the_cleanup_command_works_from_the_command_line_and_is_not_an_agent_permission(self):
        lsid, ws = self.isolated()
        self.finish(lsid)
        env = self.env(CLAUDE_TERMINAL_HANDOFF_LOGICAL_SESSION=lsid)
        proc = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "terminal_handoff", "core.py"),
                               "session", "workspace-cleanup"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, cwd=self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
        self.assertEqual(json.loads(proc.stdout.decode())["state"], "removed")
        self.assertFalse(any("workspace-cleanup" in rule for rule in CORE.agent_cli_allow_rules()))


class TestReviewFindings(ConcurrentCase):
    """Each of these was a way the first version could lose work or mislead; they must stay closed."""

    def isolated(self, name="Nova Health"):
        self.occupy(name)
        status, view = self.go()
        lsid = view["logical_session_id"]
        return lsid, self.workspace(lsid), view

    def finish(self, lsid, state=None):
        CORE.logical_mutate(lsid, lambda rec: rec.__setitem__("state", state or CORE.LS_COMPLETED))

    def test_commits_on_a_detached_head_are_never_left_unreachable(self):
        lsid, ws, _ = self.isolated()
        git(ws["path"], "checkout", "-q", "--detach")
        open(os.path.join(ws["path"], "detached.txt"), "w").write("only here\n")
        git(ws["path"], "add", "-A")
        git(ws["path"], "commit", "-q", "-m", "detached work")
        self.finish(lsid)
        outcome = CORE.cleanup_session_workspace(lsid)
        self.assertEqual(outcome["state"], "kept_unsafe", outcome)
        self.assertIn("HEAD is not on the session branch", outcome["reason"])
        self.assertTrue(os.path.exists(os.path.join(ws["path"], "detached.txt")))

    def test_a_workspace_moved_to_another_branch_is_kept(self):
        lsid, ws, _ = self.isolated()
        git(ws["path"], "checkout", "-q", "-b", "renamed-by-agent")
        self.finish(lsid)
        self.assertEqual(CORE.cleanup_session_workspace(lsid)["state"], "kept_unsafe")
        self.assertTrue(os.path.isdir(ws["path"]))

    def test_ignored_data_that_merely_sits_near_a_cache_name_is_never_deleted(self):
        os.makedirs(os.path.join(self.real_repo, "data"))
        open(os.path.join(self.real_repo, "data", "tracked.txt"), "w").write("t\n")
        git(self.real_repo, "add", "-A")
        git(self.real_repo, "commit", "-q", "-m", "data dir")
        with open(os.path.join(self.real_repo, ".git", "info", "exclude"), "a") as handle:
            handle.write("ign/\n")
        for label, relative in (("ignored dir holding a cache-named folder", "ign/node_modules/keep.txt"),
                                ("non-bytecode file in a pycache", "data/__pycache__/model.pkl")):
            with self.subTest(label):
                for record in CORE.logical_list():
                    if record["state"] not in CORE.LS_TERMINAL:
                        self.finish(record["logical_session_id"])
                lsid, ws, _ = self.isolated("Holder %s" % len(label))
                target = os.path.join(ws["path"], relative)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                open(target, "w").write("irreplaceable\n")
                self.finish(lsid)
                outcome = CORE.cleanup_session_workspace(lsid)
                self.assertEqual(outcome["state"], "kept_ignored", outcome)
                self.assertTrue(os.path.exists(target))

    def test_real_caches_under_a_tracked_directory_are_still_disposable(self):
        os.makedirs(os.path.join(self.real_repo, "src"))
        open(os.path.join(self.real_repo, "src", "m.py"), "w").write("x=1\n")
        git(self.real_repo, "add", "-A")
        git(self.real_repo, "commit", "-q", "-m", "src")
        lsid, ws, _ = self.isolated()
        os.makedirs(os.path.join(ws["path"], "src", "__pycache__"))
        open(os.path.join(ws["path"], "src", "__pycache__", "m.cpython-311.pyc"), "wb").write(b"\0")
        os.makedirs(os.path.join(ws["path"], "node_modules", "left-pad"))
        open(os.path.join(ws["path"], "node_modules", "left-pad", "i.js"), "w").write("x\n")
        open(os.path.join(ws["path"], ".DS_Store"), "wb").write(b"\0")
        with open(os.path.join(self.real_repo, ".git", "info", "exclude"), "a") as handle:
            handle.write("node_modules/\n.DS_Store\n")
        self.finish(lsid)
        self.assertEqual(CORE.cleanup_session_workspace(lsid)["state"], "removed")

    def test_an_owner_that_is_not_provably_gone_keeps_the_workspace(self):
        lsid, ws, _ = self.isolated()
        CORE.logical_mutate(lsid, lambda rec: rec.__setitem__("owner", {"agent_session_id": "agent-x", "epoch": 1, "generation": 1}))
        self.finish(lsid, CORE.LS_FAILED)
        for verdict in ("unknown", "alive"):
            outcome = CORE.cleanup_session_workspace(lsid, liveness=lambda rec, now, v=verdict: (v, "test"))
            self.assertEqual(outcome["state"], "kept_owner_alive", outcome)
            self.assertTrue(os.path.isdir(ws["path"]))
        self.assertEqual(CORE.cleanup_session_workspace(lsid, liveness=lambda rec, now: ("dead", "gone"))["state"], "removed")

    def test_a_process_running_inside_the_workspace_keeps_it_even_with_no_registered_owner(self):
        lsid, ws, _ = self.isolated()
        self.finish(lsid, CORE.LS_FAILED)  # e.g. the launch window expired while Claude was still starting
        proc = subprocess.Popen(["/bin/sleep", "120"], cwd=ws["path"])
        try:
            outcome = CORE.cleanup_session_workspace(lsid)
            self.assertEqual(outcome["state"], "kept_owner_alive", outcome)
            self.assertIn("a process is running inside the workspace", outcome["reason"])
            self.assertTrue(os.path.isdir(ws["path"]))
        finally:
            proc.kill()
            proc.wait()
        self.assertEqual(CORE.cleanup_session_workspace(lsid)["state"], "removed")

    def test_a_check_that_cannot_be_made_keeps_the_workspace(self):
        lsid, ws, _ = self.isolated()
        self.finish(lsid)
        real = CORE.workspace_in_use
        CORE.workspace_in_use = lambda path: True
        try:
            self.assertEqual(CORE.cleanup_session_workspace(lsid)["state"], "kept_owner_alive")
        finally:
            CORE.workspace_in_use = real
        self.assertTrue(os.path.isdir(ws["path"]))

    def test_the_root_recorded_at_creation_is_what_cleanup_trusts_even_if_the_config_changes(self):
        lsid, ws, _ = self.isolated()
        self.assertEqual(ws["root"], os.path.dirname(os.path.dirname(ws["path"])))
        with open(os.path.join(self.home, "remote", "config.json"), "w") as handle:
            json.dump({"worktree_root": os.path.join(self.tmp, "somewhere-else")}, handle)
        self.finish(lsid)
        self.assertEqual(CORE.cleanup_session_workspace(lsid)["state"], "removed")

    def test_files_hidden_from_git_status_keep_the_workspace(self):
        lsid, ws, _ = self.isolated()
        git(ws["path"], "update-index", "--skip-worktree", "app.py")
        with open(os.path.join(ws["path"], "app.py"), "a") as handle:
            handle.write("hidden edit\n")
        self.finish(lsid)
        outcome = CORE.cleanup_session_workspace(lsid)
        self.assertEqual(outcome["state"], "kept_unsafe", outcome)
        self.assertIn("hidden edit", open(os.path.join(ws["path"], "app.py")).read())

    def test_a_project_that_is_itself_a_linked_worktree_can_host_isolated_sessions(self):
        main = os.path.join(self.tmp, "main-checkout")
        os.makedirs(main)
        git(main, "init", "-q", "-b", "main")
        open(os.path.join(main, "f.txt"), "w").write("x\n")
        git(main, "add", "-A")
        git(main, "commit", "-q", "-m", "m")
        linked = os.path.join(self.tmp, "linked-project")
        git(main, "worktree", "add", "-q", "-b", "lp", linked)
        CORE.project_add("lp", linked)
        CORE.project_set_permissions("lp", good_profile())
        CORE.project_set_remote_launch("lp", True)
        self.other_repo = os.path.realpath(linked)
        self.held(project="lp", state=CORE.LS_RUNNING, pid=os.getpid(), name="Holder")
        status, view = self.go(project="lp", name="Side")
        self.assertEqual(status, 202, view)
        ws = self.workspace(view["logical_session_id"])
        self.assertEqual(ws["kind"], "worktree")
        self.assertIn(ws["path"], [ln[len("worktree "):] for ln in git(main, "worktree", "list", "--porcelain").splitlines() if ln.startswith("worktree ")])
        self.finish(view["logical_session_id"])
        self.assertEqual(CORE.cleanup_session_workspace(view["logical_session_id"])["state"], "removed")
        self.assertTrue(os.path.isdir(linked))

    def test_with_no_default_branch_the_phone_is_told_where_the_workspace_starts(self):
        git(self.real_repo, "branch", "-m", "main", "develop")
        git(self.real_repo, "remote", "remove", "origin")
        self.occupy()
        status, view = self.go()
        ws = self.workspace(view["logical_session_id"])
        self.assertEqual((ws["base_ref"], ws["base_sha"]), ("develop", self.base_sha))
        self.assertIn("No default branch could be identified", " ".join(view["isolation"]["warnings"]))

    def test_the_holder_named_in_the_notice_is_the_one_in_the_project_directory(self):
        canonical = self.held(state=CORE.LS_RUNNING, pid=os.getpid(), name="Canonical Holder")
        side = self.held(state=CORE.LS_RUNNING, pid=os.getpid(), name="Side Holder")
        CORE.logical_mutate(side, lambda rec: rec.__setitem__("workspace", {"kind": "worktree", "state": "ready", "path": "/nonexistent"}))
        for _ in range(3):
            status, view = self.go()
            self.assertEqual(status, 202, view)
            self.assertEqual(view["isolation"]["occupied_by"]["name"], "Canonical Holder")
            self.finish(view["logical_session_id"])
        self.assertEqual(CORE.logical_read(canonical)["state"], CORE.LS_RUNNING)

    def test_no_internal_classification_key_reaches_the_phone(self):
        import shutil
        shutil.rmtree(os.path.join(self.real_repo, ".git"))
        self.occupy()
        status, view = self.go()
        self.assertEqual(status, 409)
        self.assertNotIn("kind", view)
        self.assertNotIn("kind", view["session"] if "kind" in view["session"] else {})
        self.assertNotIn("A second session cannot share its working tree, and", view["reason"])
        self.assertIn("The project is not a Git repository, so a second session cannot be isolated safely.", view["reason"])

    def test_rules_are_re_targeted_only_on_whole_path_boundaries(self):
        rules = ["Edit(//x/app/**)", "Edit(//x/app-old/**)", "Read(//x/application/**)", "Edit(//y/x/app/**)", "Bash(ls:*)", "Edit(//x/app)", "Read(/x/app/**)"]
        allow = CORE.translate_rules_for_workspace(rules, "/x/app", "/w/ls_1")
        self.assertEqual(allow, ["Edit(//w/ls_1/**)", "Edit(//x/app-old/**)", "Read(//x/application/**)", "Edit(//y/x/app/**)", "Bash(ls:*)", "Edit(//w/ls_1)", "Read(/x/app/**)"])
        deny = CORE.translate_rules_for_workspace(rules, "/x/app", "/w/ls_1", deny=True)
        self.assertEqual(deny[:2], ["Edit(//x/app/**)", "Edit(//w/ls_1/**)"])  # kept AND re-targeted
        self.assertNotIn("Edit(//x/app-old/**)", [r for r in deny if "ls_1" in r])

    def test_planning_happens_outside_the_create_lock_when_a_holder_exists(self):
        seen = []
        real = CORE.plan_session_workspace
        real_lock = CORE._create_lock

        def watch(project_name, canonical):
            lock_fd = os.open(os.path.join(self.home, "remote", "create.lock"), os.O_WRONLY | os.O_CREAT)
            import fcntl
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # succeeds only if nobody holds the create lock
                seen.append("unlocked")
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                seen.append("locked")
            finally:
                os.close(lock_fd)
            return real(project_name, canonical)

        CORE.plan_session_workspace = watch
        try:
            self.occupy()
            status, view = self.go()
        finally:
            CORE.plan_session_workspace = real
        self.assertEqual(status, 202, view)
        self.assertEqual(seen, ["unlocked"])
        self.assertIsNotNone(real_lock)


class TestGrokUsesTheIsolatedWorkspace(GitProject, GrokCase):
    def setUp(self):
        super().setUp()
        self.init_repo()

    def occupant(self):
        record = CORE.logical_create(project="nova", repository=self.real_repo, launch_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(), launch_expires_epoch=9999999999)
        lsid = record["logical_session_id"]
        CORE.logical_register_owner(lsid, "agent-nova-health", 1, "chainNH", None, TOKEN)
        CORE.logical_mutate(lsid, lambda rec: rec.__setitem__("name", "Nova Health"))
        with open(os.path.join(self.sessions_dir, "agent-nova-health.json"), "w") as handle:
            json.dump({"pid": os.getpid(), "sessionId": "agent-nova-health"}, handle)
        return lsid

    def test_grok_is_started_inside_its_own_worktree_and_the_holder_is_untouched(self):
        holder = self.occupant()
        before = copy.deepcopy(CORE.logical_read(holder))
        fingerprint = self.fingerprint()
        lsid = self.start(task="say hello", name="PlanGuard")
        record = CORE.logical_read(lsid)
        ws = record["workspace"]
        self.assertEqual(ws["kind"], "worktree")
        self.assertEqual(record["repository"], ws["path"])
        self.assertIn(ws["path"], self.worktrees())
        self.wait_for(lambda: self.rpc("session/new"), what="Grok's session to be created")
        self.assertEqual(os.path.realpath(self.rpc("session/new")[0]["params"]["cwd"]), ws["path"])  # Grok's working directory is the workspace
        self.assertNotEqual(os.path.realpath(self.rpc("session/new")[0]["params"]["cwd"]), self.real_repo)
        self.assertEqual(CORE.logical_read(holder), before)
        self.assertEqual(self.fingerprint(), fingerprint)

    def test_a_grok_launch_on_a_free_project_still_uses_the_project_directory(self):
        lsid = self.start(task="say hello")
        self.assertEqual(CORE.logical_read(lsid)["workspace"]["kind"], "canonical")
        self.wait_for(lambda: self.rpc("session/new"), what="Grok's session to be created")
        self.assertEqual(os.path.realpath(self.rpc("session/new")[0]["params"]["cwd"]), self.real_repo)

    def test_the_bridge_refuses_to_run_if_the_isolated_workspace_disappeared(self):
        self.occupant()
        launched = []
        body = {"project": "nova", "task": "x", "agent": "grok", "request_id": "req-grok-gone-0001"}
        status, view = CORE.remote_create_session(
            body, {"device_id": "d_0123456789abcdef"}, terminal=self.fake_terminal, wait_seconds=0, isolation_check=self.isolation_check,
            grok_preflight=lambda binary: (True, None, None), grok_launcher=lambda lsid, mode: launched.append(lsid) or {"launched": True},
        )
        self.assertEqual(status, 202, view)
        lsid = view["logical_session_id"]
        self.assertEqual(CORE.logical_read(lsid)["workspace"]["state"], "ready")
        git(self.real_repo, "worktree", "remove", "--force", CORE.logical_read(lsid)["workspace"]["path"])
        bridge = CORE.GrokBridge(lsid, "start")
        self.assertEqual(bridge.run(), 1)
        self.assertEqual(CORE.logical_read(lsid)["state"], CORE.LS_FAILED)


if __name__ == "__main__":
    unittest.main()
