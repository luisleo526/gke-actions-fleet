import json
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / "examples/member-only-pr.yml").read_text())
SCRIPT = WORKFLOW["jobs"]["membership"]["steps"][1]["with"]["script"]
HARNESS = r"""
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const outputs = {};
const calls = [];
const core = {setOutput: (key, value) => { outputs[key] = value; }};
const github = {rest: {orgs: {getMembershipForUser: async ({org, username}) => {
  calls.push({org, username});
  const result = input.members[username];
  if (!result || result.error) {
    const error = new Error('membership lookup failed');
    error.status = result?.error || 404;
    throw error;
  }
  return {data: {state: result.state}};
}}}};
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
(async () => {
  let failed = false;
  try {
    await new AsyncFunction('core', 'github', 'context', 'process', input.script)(
      core, github, input.context, {env: {TRIGGERING_ACTOR: input.triggering_actor}});
  } catch { failed = true; }
  console.log(JSON.stringify({outputs, calls, failed}));
})();
"""


class MembershipGateTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "script": SCRIPT,
            "context": {
                "repo": {"owner": "example-org", "repo": "example-repo"},
                "actor": "member-b",
                "payload": {
                    "pull_request": {
                        "draft": False,
                        "user": {"login": "member-a"},
                        "head": {"sha": "a" * 40},
                        "base": {"repo": {"owner": {"type": "Organization"}, "full_name": "example-org/example-repo"}},
                    }
                },
            },
            "triggering_actor": "member-c",
            "members": {name: {"state": "active"} for name in ["member-a", "member-b", "member-c"]},
        }

    def execute(self):
        result = subprocess.run(
            ["node", "-e", HARNESS], input=json.dumps(self.data), text=True, capture_output=True, check=True, timeout=10
        )
        return json.loads(result.stdout)

    def test_active_members_admit_exact_head_without_exporting_token(self):
        result = self.execute()
        self.assertFalse(result["failed"])
        self.assertEqual(result["outputs"], {"allowed": "true", "head_sha": "a" * 40})
        self.assertEqual({c["username"] for c in result["calls"]}, {"member-a", "member-b", "member-c"})

    def test_nonmember_pr_author_is_denied(self):
        self.data["members"]["member-a"] = {"error": 404}
        result = self.execute()
        self.assertEqual(result["outputs"], {"allowed": "false"})
        self.assertFalse(result["failed"])

    def test_pending_invitation_is_denied(self):
        self.data["members"]["member-a"] = {"state": "pending"}
        self.assertEqual(self.execute()["outputs"], {"allowed": "false"})

    def test_nonmember_event_actor_is_denied(self):
        del self.data["members"]["member-b"]
        self.assertEqual(self.execute()["outputs"], {"allowed": "false"})

    def test_nonmember_rerun_actor_is_denied(self):
        del self.data["members"]["member-c"]
        self.assertEqual(self.execute()["outputs"], {"allowed": "false"})

    def test_api_permission_failure_or_outage_fails_closed(self):
        for status in [403, 429, 500]:
            with self.subTest(status=status):
                self.data["members"]["member-a"] = {"error": status}
                result = self.execute()
                self.assertTrue(result["failed"])
                self.assertEqual(result["outputs"], {"allowed": "false"})

    def test_draft_and_wrong_repository_do_not_query_membership(self):
        self.data["context"]["payload"]["pull_request"]["draft"] = True
        self.assertEqual(self.execute()["calls"], [])
        self.data["context"]["payload"]["pull_request"]["draft"] = False
        self.data["context"]["payload"]["pull_request"]["base"]["repo"]["full_name"] = "other/repo"
        self.assertEqual(self.execute()["calls"], [])

    def test_admission_has_no_checkout_and_worker_has_no_credential_inheritance(self):
        gate, worker = WORKFLOW["jobs"]["membership"], WORKFLOW["jobs"]["test"]
        self.assertFalse(any("actions/checkout" in step.get("uses", "") for step in gate["steps"]))
        self.assertNotIn("secrets", worker)
        self.assertEqual(worker["permissions"], {"contents": "read"})
        self.assertFalse(worker["steps"][0]["with"]["persist-credentials"])
        self.assertEqual(gate["environment"], "gke-pr-admission")
        self.assertNotIn("environment", worker)


if __name__ == "__main__":
    unittest.main()
