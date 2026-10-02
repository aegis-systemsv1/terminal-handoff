"""The Nova operator grant (owner decision, 30 September 2026).

Proves both halves: the `nova` project's nova-operator profile can push its own
feature branch, merge a green PR and deploy an exact merged SHA through `nova-ops`,
and no other project (nor a Grok session, nor a tampered profile) gains any of it.
GitHub, and Nova's own deploy tool, are faked; git is real, on temporary repos.
"""

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import CORE, text_file  # noqa: E402
from test_logical import LogicalCase  # noqa: E402
from test_remote_api import good_profile  # noqa: E402
from test_remote_launch import LaunchCase  # noqa: E402


def nova_profile(**overrides):
    profile = {
        "profile": "nova-operator",
        "elevation": "nova-operator",
        "description": "Nova sessions ship without John at a Terminal, through nova-ops only.",
        "allow": [
            "Read",
            "Grep",
            "Glob",
            "Edit(scripts/**)",
            "Edit(tools/**)",
            "Bash(git status:*)",
            "Bash(git diff:*)",
            "Bash(git log:*)",
            "Bash(git add:*)",
            "Bash(git commit:*)",
            "Bash(gh pr create:*)",
            "Bash(gh pr view:*)",
            "Bash(gh pr checks:*)",
        ],
        "deny": list(CORE.NOVA_OPERATOR_REQUIRED_DENY) + ["Bash(rm:*)", "Bash(curl:*)"],
        "human_gate": [g for g in CORE.MANDATORY_HUMAN_GATES if g not in CORE.NOVA_OPERATOR_WAIVED_GATES]
        + [CORE.NOVA_OPERATOR_SCOPED_GATE],
    }
    profile.update(overrides)
    return profile


def git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), check=True, capture_output=True, text=True).stdout.strip()


class TestNovaOperatorProfile(LogicalCase):
    def setUp(self):
        super().setUp()
        for name in ("nova", "other"):
            os.makedirs(os.path.join(self.tmp, name))
            CORE.project_add(name, os.path.join(self.tmp, name))

    def test_the_nova_profile_validates_for_the_nova_project(self):
        self.assertEqual(CORE.validate_permission_profile(nova_profile(), "nova"), [])
        ok, problems = CORE.project_set_permissions("nova", nova_profile())
        self.assertTrue(ok, problems)
        self.assertTrue(CORE.project_set_remote_launch("nova", True)[0])
        profile, why = CORE.project_permissions_ok(CORE.projects_load()["nova"], "nova")
        self.assertIsNone(why)
        self.assertTrue(CORE.profile_is_nova_operator(profile, "nova"))

    def test_the_nova_profile_may_drop_only_the_deployment_gates(self):
        gates = nova_profile()["human_gate"]
        for waived in ("production deployment", "production restart", "rollback"):
            self.assertNotIn(waived, gates)
        for kept in ("destructive filesystem operation", "credential changes", "security configuration changes",
                     "irreversible external action"):
            self.assertIn(kept, gates)
            with self.subTest(kept):
                self.assertTrue(CORE.validate_permission_profile(
                    nova_profile(human_gate=[g for g in gates if g != kept]), "nova"))

    def test_no_other_project_can_use_the_elevation(self):
        for name in ("other", "terminal-handoff", "scratch", None):
            with self.subTest(name):
                problems = CORE.validate_permission_profile(nova_profile(), name)
                self.assertTrue(any("belongs only to project" in p for p in problems), problems)
        self.assertFalse(CORE.project_set_permissions("other", nova_profile())[0])
        self.assertIsNone(CORE.projects_load()["other"]["permissions"])

    def test_other_projects_keep_every_mandatory_gate(self):
        for gate in CORE.MANDATORY_HUMAN_GATES:
            with self.subTest(gate):
                dropped = good_profile(human_gate=[g for g in CORE.MANDATORY_HUMAN_GATES if g != gate])
                self.assertTrue(CORE.validate_permission_profile(dropped, "other"))
                # Nor may nova drop a gate without asking for the elevation by name.
                self.assertTrue(CORE.validate_permission_profile(dropped, "nova"))

    def test_the_elevation_must_carry_its_scoped_gate_and_deny_rules(self):
        cases = {
            "unknown elevation": nova_profile(elevation="root"),
            "wrong profile name": nova_profile(profile="development"),
            "no scoped gate": nova_profile(human_gate=[g for g in nova_profile()["human_gate"] if g != CORE.NOVA_OPERATOR_SCOPED_GATE]),
        }
        for rule in CORE.NOVA_OPERATOR_REQUIRED_DENY:
            cases["missing deny %s" % rule] = nova_profile(deny=[r for r in nova_profile()["deny"] if r != rule])
        for label, profile in cases.items():
            with self.subTest(label):
                self.assertTrue(CORE.validate_permission_profile(profile, "nova"), label)
                self.assertFalse(CORE.project_set_permissions("nova", profile)[0])

    def test_the_elevation_never_pre_approves_raw_push_merge_or_sudo(self):
        for rule in ("Bash(git push:*)", "Bash(git push --force:*)", "Bash(sudo:*)", "Bash(sudo -n /Library/Nova/bin/nova-privd:*)",
                     "Bash(rm:*)", "Bash(curl:*)", "Bash(vercel:*)"):
            with self.subTest(rule):
                profile = nova_profile(allow=nova_profile()["allow"] + [rule])
                self.assertTrue(CORE.validate_permission_profile(profile, "nova"))

    def test_an_elevated_profile_copied_to_another_project_fails_closed(self):
        CORE.project_set_permissions("other", good_profile())
        CORE.project_set_remote_launch("other", True)

        def tamper(projects):
            profile = nova_profile()
            projects["other"]["permissions"] = {"profile": profile, "validated_sha256": CORE.permission_profile_hash(profile)}

        CORE.projects_update(tamper)
        profile, why = CORE.project_permissions_ok(CORE.projects_load()["other"], "other")
        self.assertIsNone(profile)
        self.assertIn("belongs only to project", why)

    def test_without_elevation_restores_every_mandatory_gate(self):
        plain = CORE.without_elevation(nova_profile())
        self.assertNotIn("elevation", plain)
        for gate in CORE.MANDATORY_HUMAN_GATES:
            self.assertIn(gate, plain["human_gate"])
        self.assertEqual(CORE.validate_permission_profile(plain, "other"), [])
        self.assertIs(CORE.without_elevation(good_profile()).get("elevation"), None)

    def test_nova_ops_refuses_while_nova_runs_an_ordinary_profile(self):
        CORE.project_set_permissions("nova", good_profile())
        with self.assertRaises(CORE.NovaOpsRefused):
            CORE.nova_ops_canonical()


class TestNovaOperatorLaunch(LaunchCase):
    def settings_of(self, view):
        record = CORE.logical_read(view["logical_session_id"])
        return json.loads(text_file(record["permissions"]["settings_file"])), record

    def test_a_nova_operator_session_gets_nova_ops_and_keeps_its_denies(self):
        CORE.project_set_permissions("nova", nova_profile())
        CORE.project_set_remote_launch("nova", True)
        status, view = self.create(task="Ship the fix.")
        self.assertEqual(status, 201, view)
        settings, record = self.settings_of(view)
        allow, deny = settings["permissions"]["allow"], settings["permissions"]["deny"]
        for rule in CORE.nova_ops_allow_rules():
            self.assertIn(rule, allow)
        self.assertTrue(CORE.nova_ops_allow_rules())
        for rule in CORE.NOVA_OPERATOR_REQUIRED_DENY:
            self.assertIn(rule, deny)
        self.assertIn("Edit(scripts/**)", allow)
        self.assertIn("Edit(tools/**)", allow)
        self.assertNotIn("production deployment", record["permissions"]["human_gate"])
        self.assertEqual(settings["permissions"]["disableBypassPermissionsMode"], "disable")
        prompt = text_file(CORE.th_path("prompts", "remote-%s.md" % view["logical_session_id"]))
        self.assertIn("NOVA OPERATOR", prompt)
        self.assertIn("nova-ops deploy SHA --instruction", prompt)

    def test_an_ordinary_profile_session_gets_nothing_extra(self):
        status, view = self.create()
        self.assertEqual(status, 201, view)
        settings, record = self.settings_of(view)
        self.assertFalse([r for r in settings["permissions"]["allow"] if "nova-ops" in r])
        for gate in CORE.MANDATORY_HUMAN_GATES:
            self.assertIn(gate, record["permissions"]["human_gate"])
        prompt = text_file(CORE.th_path("prompts", "remote-%s.md" % view["logical_session_id"]))
        self.assertNotIn("NOVA OPERATOR", prompt)

    def test_another_project_never_gets_nova_ops(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        CORE.project_add("other", other)
        CORE.project_set_permissions("other", good_profile())
        CORE.project_set_remote_launch("other", True)
        CORE.project_set_permissions("nova", nova_profile())
        CORE.project_set_remote_launch("nova", True)
        status, view = self.create(project="other")
        self.assertIn(status, (201, 202), view)  # the fake registers in nova's directory; settings are written first
        settings, record = self.settings_of(view)
        self.assertFalse([r for r in settings["permissions"]["allow"] if "nova-ops" in r])
        self.assertIn("production deployment", record["permissions"]["human_gate"])


class NovaOpsCase(LogicalCase):
    """A real Nova-shaped repo, a local bare origin, and a fake GitHub + fake Nova tool."""

    def setUp(self):
        super().setUp()
        self.origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True)
        self.repo = os.path.join(self.tmp, "Nova")
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
        for key, value in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
            git(self.repo, "config", key, value)
        os.makedirs(os.path.join(self.repo, "scripts"))
        self.calls_file = os.path.join(self.tmp, "tool-calls.jsonl")
        with open(os.path.join(self.repo, "scripts", "nova_deploy.py"), "w") as handle:
            handle.write(
                "import json, os, sys\n"
                "with open(%r, 'a') as f:\n"
                "    f.write(json.dumps({'argv': sys.argv[1:], 'tool': os.path.abspath(__file__), 'cwd': os.getcwd(),\n"
                "                        'nova_env': sorted(k for k in os.environ if k.startswith('NOVA_'))}) + '\\n')\n"
                "sys.exit(int(os.environ.get('FAKE_TOOL_EXIT', '0')))\n" % self.calls_file
            )
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "remote", "add", "origin", self.origin)
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")
        self.main_sha = git(self.repo, "rev-parse", "origin/main")
        CORE.project_add("nova", self.repo)
        CORE.project_set_permissions("nova", nova_profile())
        self.gh_calls = []
        self.checks = [{"name": n, "status": "completed", "conclusion": "success"} for n in CORE.NOVA_REQUIRED_CHECKS]
        self.pr = {}

    def runner(self, argv, **kwargs):
        """GitHub is faked, fetch is local, the origin URL looks like GitHub; everything else is real."""
        if argv[0] == "gh":
            self.gh_calls.append(argv)
            if argv[1] == "api" and argv[2].startswith("repos/aegis/Nova/commits/") and argv[2].endswith("/check-runs?per_page=100"):
                return subprocess.CompletedProcess(argv, 0, json.dumps({"check_runs": self.checks}), "")
            if argv[1:3] == ["pr", "view"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps(self.pr), "")
            if argv[1:3] == ["pr", "merge"]:
                self.pr.update(state="MERGED", mergeCommit={"oid": "m" * 40})
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 1, "", "unexpected gh call")
        if argv[:1] == ["git"] and argv[3:5] == ["remote", "get-url"]:
            return subprocess.CompletedProcess(argv, 0, "git@github.com:aegis/Nova.git\n", "")
        return subprocess.run(argv, **kwargs)

    def tool_calls(self):
        if not os.path.exists(self.calls_file):
            return []
        with open(self.calls_file) as handle:
            return [json.loads(line) for line in handle]


class TestNovaOpsPush(NovaOpsCase):
    def test_pushes_the_checked_out_feature_branch(self):
        git(self.repo, "switch", "-q", "-c", "feat/x")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "x")
        result = CORE.nova_ops_push(self.repo)
        self.assertEqual(result["pushed"], "feat/x")
        self.assertEqual(git(self.origin, "rev-parse", "refs/heads/feat/x"), git(self.repo, "rev-parse", "HEAD"))

    def test_pushes_from_a_worktree_of_the_project(self):
        tree = os.path.join(self.tmp, "wt")
        git(self.repo, "worktree", "add", "-q", "-b", "feat/wt", tree)
        CORE.nova_ops_push(tree)
        self.assertTrue(git(self.origin, "rev-parse", "refs/heads/feat/wt"))

    def test_refuses_main_and_detached_head(self):
        with self.assertRaisesRegex(CORE.NovaOpsRefused, "never main"):
            CORE.nova_ops_push(self.repo)
        git(self.repo, "switch", "-q", "--detach")
        with self.assertRaisesRegex(CORE.NovaOpsRefused, "detached"):
            CORE.nova_ops_push(self.repo)

    def test_never_forces(self):
        git(self.repo, "switch", "-q", "-c", "feat/y")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "one")
        CORE.nova_ops_push(self.repo)
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "rewritten")
        with self.assertRaisesRegex(CORE.NovaOpsRefused, "push failed"):
            CORE.nova_ops_push(self.repo)

    def test_refuses_a_repository_that_is_not_nova(self):
        other = os.path.join(self.tmp, "elsewhere")
        subprocess.run(["git", "init", "-q", "-b", "feat/z", other], check=True)
        with self.assertRaisesRegex(CORE.NovaOpsRefused, "not a checkout"):
            CORE.nova_ops_push(other)


class TestNovaOpsMerge(NovaOpsCase):
    def green_pr(self, **overrides):
        pr = {"number": 7, "state": "OPEN", "isDraft": False, "baseRefName": "main", "headRefName": "feat/x",
              "headRefOid": "a" * 40, "mergeStateStatus": "CLEAN", "reviewDecision": "", "isCrossRepository": False,
              "statusCheckRollup": [{"name": n, "status": "COMPLETED", "conclusion": "SUCCESS"} for n in CORE.NOVA_REQUIRED_CHECKS]}
        pr.update(overrides)
        return pr

    def test_merges_a_green_pr_pinned_to_its_head(self):
        self.pr = self.green_pr()
        result = CORE.nova_ops_merge(7, "a" * 40, runner=self.runner)
        self.assertEqual(result["state"], "MERGED")
        merge = [c for c in self.gh_calls if c[1:3] == ["pr", "merge"]][0]
        self.assertIn("--match-head-commit", merge)
        self.assertIn("a" * 40, merge)
        self.assertNotIn("--admin", merge)

    def test_refuses_anything_not_green_and_exact(self):
        red = [{"name": "Architecture Validation", "status": "COMPLETED", "conclusion": "FAILURE"},
               {"name": "Safety Boundary Gate", "status": "COMPLETED", "conclusion": "SUCCESS"}]
        cases = {
            "failing CI": self.green_pr(statusCheckRollup=red, mergeStateStatus="UNSTABLE"),
            "missing CI": self.green_pr(statusCheckRollup=[]),
            "running CI": self.green_pr(statusCheckRollup=[{"name": "Safety Boundary Gate", "status": "IN_PROGRESS"}]),
            "moved head": self.green_pr(headRefOid="b" * 40),
            "draft": self.green_pr(isDraft=True),
            "not main": self.green_pr(baseRefName="release"),
            "fork": self.green_pr(isCrossRepository=True),
            "changes requested": self.green_pr(reviewDecision="CHANGES_REQUESTED"),
            "closed": self.green_pr(state="CLOSED"),
            "dirty": self.green_pr(mergeStateStatus="DIRTY"),
        }
        for label, pr in cases.items():
            with self.subTest(label):
                self.pr, self.gh_calls = pr, []
                with self.assertRaises(CORE.NovaOpsRefused):
                    CORE.nova_ops_merge(7, "a" * 40, runner=self.runner)
                self.assertFalse([c for c in self.gh_calls if c[1:3] == ["pr", "merge"]], label)

    def test_requires_a_full_sha(self):
        with self.assertRaises(CORE.NovaOpsRefused):
            CORE.nova_ops_merge(7, "main", runner=self.runner)


class TestNovaOpsDeploy(NovaOpsCase):
    INSTRUCTION = "John: deploy this fix to production now."

    def deploy(self, sha, **kwargs):
        return CORE.nova_ops_deploy(sha, "automation-core", self.INSTRUCTION, runner=self.runner,
                                    sleep=lambda s: None, python=sys.executable, **kwargs)

    def test_deploys_an_exact_merged_sha_with_the_tool_from_a_clean_archive(self):
        # An uncommitted edit to the tool in the checkout must never be what runs.
        with open(os.path.join(self.repo, "scripts", "nova_deploy.py"), "a") as handle:
            handle.write("raise SystemExit('the editable checkout ran')\n")
        os.environ["NOVA_SOURCE_REPO"] = "/tmp/redirected"
        try:
            result = self.deploy(self.main_sha)
        finally:
            os.environ.pop("NOVA_SOURCE_REPO")
        self.assertTrue(result["verified"])
        calls = self.tool_calls()
        self.assertEqual([c["argv"][0] for c in calls], ["authorize", "deploy", "verify"])
        authorize, deploy = calls[0]["argv"], calls[1]["argv"]
        self.assertEqual(authorize[1], self.main_sha)
        self.assertIn(self.INSTRUCTION, authorize)
        self.assertEqual(deploy, ["deploy", self.main_sha, "--service-key", "automation-core"])
        for call in calls:
            self.assertFalse(call["tool"].startswith(os.path.realpath(self.repo)), call["tool"])
            self.assertEqual(call["nova_env"], [])  # NOVA_* overrides never reach the tool
        self.assertFalse(os.listdir(CORE.th_path("state", "nova-ops")))  # the archive is cleaned up
        for flag in ("--allow-unmerged", "--no-rollback", "--allowed-ref", "--no-fetch"):
            self.assertNotIn(flag, deploy)

    def test_refuses_a_sha_not_in_origin_main(self):
        git(self.repo, "switch", "-q", "-c", "feat/unmerged")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "unmerged")
        with self.assertRaisesRegex(CORE.NovaOpsRefused, "not contained in origin/main"):
            self.deploy(git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(self.tool_calls(), [])

    def test_refuses_a_ref_or_short_sha(self):
        for target in ("main", self.main_sha[:12], "HEAD"):
            with self.subTest(target), self.assertRaisesRegex(CORE.NovaOpsRefused, "40-character"):
                self.deploy(target)
        self.assertEqual(self.tool_calls(), [])

    def test_refuses_without_the_owner_instruction(self):
        with self.assertRaisesRegex(CORE.NovaOpsRefused, "instruction"):
            CORE.nova_ops_deploy(self.main_sha, "automation-core", "deploy", runner=self.runner, python=sys.executable)
        self.assertEqual(self.tool_calls(), [])

    def test_refuses_on_red_or_missing_ci(self):
        for label, checks in (("red", [{"name": "Architecture Validation", "status": "completed", "conclusion": "failure"},
                                       {"name": "Safety Boundary Gate", "status": "completed", "conclusion": "success"}]),
                              ("missing", [])):
            with self.subTest(label):
                self.checks = checks
                with self.assertRaisesRegex(CORE.NovaOpsRefused, "CI gate"):
                    self.deploy(self.main_sha, wait_ci=0)
        self.assertEqual(self.tool_calls(), [])

    def test_a_failed_deploy_is_reported_as_failed(self):
        os.environ["FAKE_TOOL_EXIT"] = "2"
        try:
            with self.assertRaisesRegex(CORE.NovaOpsRefused, "refused to record"):
                self.deploy(self.main_sha)
        finally:
            os.environ.pop("FAKE_TOOL_EXIT")

    def test_rollback_uses_the_origin_main_tool_and_passes_the_purpose(self):
        CORE.nova_ops_deploy(self.main_sha, "automation-core", self.INSTRUCTION, rollback=True, runner=self.runner,
                             python=sys.executable)
        calls = self.tool_calls()
        self.assertEqual(calls[0]["argv"][-1], "--rollback")
        self.assertEqual(calls[1]["argv"][:2], ["rollback", self.main_sha])

    def test_the_request_is_attributed_in_the_log(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sess-attrib-1"
        try:
            self.deploy(self.main_sha)
        finally:
            os.environ.pop("CLAUDE_CODE_SESSION_ID")
        rows = [json.loads(line) for line in open(CORE.log_path()) if "nova_ops_deploy" in line]
        self.assertTrue(any(r.get("claude_session") == "sess-attrib-1" and r.get("instruction_sha256") for r in rows))
        self.assertTrue(any(r["event"] == "nova_ops_deploy" and r["outcome"] == "ok" for r in rows))


class TestNovaArchiveMembers(unittest.TestCase):
    """The checks tarfile's "data" filter makes, enforced on every supported Python."""

    def member(self, name, kind="file", link=""):
        import tarfile

        info = tarfile.TarInfo(name)
        info.type = {"file": tarfile.REGTYPE, "dir": tarfile.DIRTYPE, "sym": tarfile.SYMTYPE,
                     "hard": tarfile.LNKTYPE, "dev": tarfile.CHRTYPE, "fifo": tarfile.FIFOTYPE}[kind]
        info.linkname = link
        return info

    def setUp(self):
        import tempfile

        self.root = tempfile.mkdtemp()

    def test_an_ordinary_tree_with_an_in_tree_relative_symlink_is_accepted(self):
        # Shape of Nova's own tree: .claude/skills/find-skills -> ../../.agents/skills/find-skills
        CORE.nova_check_archive_members([
            self.member("scripts", "dir"), self.member("scripts/nova_deploy.py"),
            self.member(".claude/skills/find-skills", "sym", "../../.agents/skills/find-skills"),
            self.member("docs/copy.md", "hard", "docs/original.md"),
        ], self.root)

    def test_anything_that_could_escape_or_is_not_a_plain_entry_is_refused(self):
        cases = [
            self.member("/etc/passwd"),
            self.member("../outside"),
            self.member("a/../../outside"),
            self.member("link", "sym", "/etc"),
            self.member("link", "sym", "../../outside"),
            self.member("hard", "hard", "../outside"),
            self.member("dev", "dev"),
            self.member("pipe", "fifo"),
        ]
        for member in cases:
            with self.subTest(member.name, link=member.linkname):
                with self.assertRaises(CORE.NovaOpsRefused):
                    CORE.nova_check_archive_members([member], self.root)

    def test_extraction_works_without_the_data_filter(self):
        """Python 3.9 has no `filter=`: extraction must still succeed there, validated."""
        repo = os.path.join(self.root, "repo")
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
        os.makedirs(os.path.join(repo, "scripts"))
        with open(os.path.join(repo, "scripts", "nova_deploy.py"), "w") as handle:
            handle.write("print('tool')\n")
        os.symlink("scripts/nova_deploy.py", os.path.join(repo, "alias"))
        git(repo, "add", ".")
        git(repo, "-c", "user.email=t@e", "-c", "user.name=T", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "x")
        sha = git(repo, "rev-parse", "HEAD")
        dest = os.path.join(self.root, "work")
        os.makedirs(dest)
        real = CORE.nova_tar_has_data_filter
        CORE.nova_tar_has_data_filter = lambda: False
        try:
            tree = CORE.nova_extract_tree(repo, sha, dest)
        finally:
            CORE.nova_tar_has_data_filter = real
        self.assertTrue(os.path.isfile(os.path.join(tree, "scripts", "nova_deploy.py")))
        self.assertEqual(os.readlink(os.path.join(tree, "alias")), "scripts/nova_deploy.py")


class TestNovaPrivd(unittest.TestCase):
    def test_only_self_gated_verbs_with_well_formed_values(self):
        self.assertEqual(CORE.nova_privd_argv("status", ["--json"]), ["sudo", "-n", CORE.NOVA_PRIVD, "status", "--json"])
        self.assertEqual(CORE.nova_privd_argv("restart", ["--service", "watchdog"])[-2:], ["--service", "watchdog"])
        for verb, args in (("approve", []), ("install", []), ("set-policy", []), ("break-glass-deploy", []),
                           ("deploy", ["--receipt", "../../etc"]), ("restart", ["--service", "sshd"]),
                           ("status", ["--json", ";", "rm"]), ("request-mint", ["--sha", "main"])):
            with self.subTest(verb=verb, args=args), self.assertRaises(CORE.NovaOpsRefused):
                CORE.nova_privd_argv(verb, args)


if __name__ == "__main__":
    unittest.main()
