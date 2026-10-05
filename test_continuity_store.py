"""Synthetic receipts only: no reads of installed AI applications or user logs."""
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from core.continuity_store import ContinuityStore


class ContinuityStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "continuity.db"
        self.store = ContinuityStore(self.path)
        self.source = self.store.add_source("codex", str(Path(self.directory.name) / "logs"))
        self.source_id = self.source["source_id"]

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def event(self, event_id="e1", kind="plan", **values):
        event = {"event_id": event_id, "session_id": "session-one", "kind": kind,
                 "timestamp": "2026-09-01T00:00:00Z", "workspace_path": "project-one",
                 "payload": {"plan_id": "plan-one", "goal": "Build a feature", "steps": [
                     {"id": "a", "description": "Implement", "status": "in_progress"},
                     {"id": "b", "description": "Validate", "status": "pending", "dependencies": ["a"]}]},
                 "source_ref": {"file_path": "synthetic.jsonl", "line": 1}}
        event.update(values)
        return event

    def ingest(self, events, cursor=None, source_id=None):
        return self.store.ingest_batch(source_id or self.source_id, "synthetic.jsonl", events, cursor or {"offset": 100})

    def step(self, external_id):
        details = self.store.session_detail(self.store.list_sessions()[0]["session_key"])
        return next(row for row in details["steps"] if row["external_id"] == external_id)

    def test_idempotent_events_and_cursor_survive_reopen(self):
        self.assertEqual(1, self.ingest([self.event()]))
        self.assertEqual(0, self.ingest([self.event()], {"offset": 200}))
        self.store.close()
        self.store = ContinuityStore(self.path)
        self.assertEqual({"offset": 200}, self.store.get_cursor(self.source_id, "synthetic.jsonl"))
        detail = self.store.session_detail(self.store.list_sessions()[0]["session_key"])
        self.assertEqual(1, len(detail["events"]))
        self.assertEqual(1, len(detail["plans"][0]["versions"]))
        self.assertEqual(2, len(detail["steps"]))

    def test_malformed_batch_does_not_advance_cursor_or_partial_projection(self):
        self.ingest([self.event()], {"offset": 50})
        good = self.event("good", session_id="another-session")
        bad = self.event("bad", payload={"steps": "not-a-list"})
        with self.assertRaises(ValueError):
            self.ingest([good, bad], {"offset": 900})
        self.assertEqual({"offset": 50}, self.store.get_cursor(self.source_id, "synthetic.jsonl"))
        self.assertEqual(1, len(self.store.list_sessions()))

    def test_reported_completion_requires_confirmation_before_dependency_runs(self):
        event = self.event()
        event["payload"]["steps"][0]["status"] = "completed"
        self.ingest([event])
        board = self.store.dashboard()
        self.assertEqual([], board["next_actions"])
        self.assertEqual(1, board["stats"]["reported_done"])
        self.assertIn("확인", board["blocked"][0]["waiting_reason"])
        completed = self.store.update_step(self.step("a")["id"], status="done")
        self.assertEqual("user_confirmed", completed["verification"])
        self.assertEqual("Validate", self.store.dashboard()["next_actions"][0]["description"])
        self.assertEqual(1, self.store.dashboard()["stats"]["confirmed_done"])

    def test_internal_verified_receipts_are_preserved(self):
        event = self.event()
        event["payload"]["steps"][0].update(status="done", verification="verified", evidence=[{"test": "passed"}])
        self.ingest([event])
        self.assertEqual([{"test": "passed"}], self.step("a")["evidence"]["receipts"])
        self.assertEqual("Validate", self.store.dashboard()["next_actions"][0]["description"])

    def test_overrides_survive_new_versions_and_history_remains_immutable(self):
        self.ingest([self.event()])
        step = self.step("a")
        self.store.update_step(step["id"], status="blocked", priority=3, due_at="2026-10-01T12:00:00+09:00")
        revised = self.event("e2", timestamp="2026-09-02T00:00:00Z")
        revised["payload"]["steps"][0]["status"] = "done"
        self.ingest([revised])
        current = self.step("a")
        self.assertEqual("blocked", current["status"])
        self.assertEqual("done", current["reported_status"])
        self.assertEqual(3, current["priority"])
        self.assertTrue(current["due_at"].startswith("2026-10-01T03:00:00"))
        detail = self.store.session_detail(current["session_key"])
        self.assertEqual("in_progress", detail["plans"][0]["versions"][0]["payload"]["steps"][0]["status"])
        self.assertEqual(2, len(detail["plans"][0]["versions"]))
        self.assertEqual(1, len(detail["history"]))
        self.store.update_step(step["id"], due_at=None)
        self.assertIsNone(self.step("a")["due_at"])

    def test_out_of_order_receipt_cannot_regress_plan(self):
        current = self.event("new", timestamp="2026-09-03T00:00:00Z")
        current["payload"]["steps"][0]["status"] = "done"
        self.ingest([current, self.event("old")])
        self.assertEqual("done", self.step("a")["status"])
        details = self.store.session_detail(self.step("a")["session_key"])
        self.assertEqual(2, len(details["plans"][0]["versions"]))

    def test_plan_replacement_removes_old_active_steps_without_losing_history(self):
        self.ingest([self.event()])
        revised = self.event("revised", timestamp="2026-09-03T00:00:00Z")
        revised["payload"]["steps"] = [{"id": "c", "description": "New scope"}]
        self.ingest([revised])
        detail = self.store.session_detail(self.store.list_sessions()[0]["session_key"])
        self.assertEqual(["c"], [step["external_id"] for step in detail["steps"]])
        self.assertEqual(2, len(detail["plans"][0]["versions"]))

    def test_task_patches_preserve_other_tasks_and_fields(self):
        first = self.event("task-a", "task", payload={"task_id": "a", "description": "First", "status": "pending"})
        second = self.event("task-b", "task", payload={"task_id": "b", "description": "Second", "dependencies": ["a"]})
        patch = self.event("patch-a", "task", timestamp="2026-09-02T00:00:00Z", payload={"task_id": "a", "status": "done"})
        self.ingest([first, second, patch])
        self.assertEqual("First", self.step("a")["description"])
        self.assertEqual("done", self.step("a")["status"])
        self.assertEqual("Second", self.step("b")["description"])
        self.assertEqual(["a"], self.step("b")["dependencies"])

    def test_delayed_task_patch_uses_its_own_timestamp_not_another_tasks(self):
        first = self.event("task-a-pending", "task", timestamp="2026-09-01T00:00:01Z",
                           payload={"task_id": "a", "description": "First", "status": "pending"})
        second = self.event("task-b-pending", "task", timestamp="2026-09-01T00:00:03Z",
                            payload={"task_id": "b", "description": "Second", "status": "pending"})
        delayed = self.event("task-a-completed", "task", timestamp="2026-09-01T00:00:02Z",
                             payload={"task_id": "a", "status": "completed"})
        for event in (first, second, delayed):
            self.ingest([event])

        self.assertEqual("done", self.step("a")["status"])
        self.assertEqual("First", self.step("a")["description"])
        self.assertEqual("pending", self.step("b")["status"])
        self.assertEqual("Second", self.step("b")["description"])
        detail = self.store.session_detail(self.step("a")["session_key"])
        self.assertEqual(3, len(detail["plans"][0]["versions"]))
        self.assertTrue(detail["plans"][0]["updated_at"].startswith("2026-09-01T00:00:03"))

    def test_sources_and_sessions_do_not_merge_on_workspace(self):
        other = self.store.add_source("claude_code", str(Path(self.directory.name) / "claude"))
        self.ingest([self.event()])
        self.ingest([self.event()], source_id=other["source_id"])
        self.ingest([self.event("other-session", session_id="two")])
        self.assertEqual(3, len(self.store.list_sessions()))
        self.assertEqual(6, self.store.dashboard()["stats"]["steps"])
        self.assertEqual(1, len(self.store.dashboard()["projects"]))

    def test_messages_are_suggestions_only_until_user_promotes(self):
        message = self.event("message", "message", role="assistant", text="I finished everything. Next deploy!", payload={})
        self.ingest([message])
        board = self.store.dashboard()
        self.assertEqual([], board["next_actions"])
        self.assertEqual([], board["recent"])
        self.assertEqual(1, len(board["suggested"]))
        session_key = board["suggested"][0]["session_key"]
        step = self.store.confirm_suggestion(session_key, "Check what remains")
        self.assertEqual("user_confirmed", step["verification"])
        self.assertEqual("Check what remains", self.store.dashboard()["next_actions"][0]["description"])
        self.store.confirm_suggestion(session_key)
        self.assertEqual(1, self.store.dashboard()["stats"]["steps"])
        self.assertEqual([], self.store.dashboard()["suggested"])

    def test_disable_and_purge_affect_only_copied_data(self):
        self.ingest([self.event()])
        self.store.set_source_enabled(self.source_id, False)
        self.assertEqual([], self.store.dashboard()["next_actions"])
        self.assertEqual(1, len(self.store.list_sessions()))
        self.assertEqual(0, self.ingest([self.event("skipped")], {"offset": 999}))
        self.assertEqual({"offset": 100}, self.store.get_cursor(self.source_id, "synthetic.jsonl"))
        self.store.set_source_enabled(self.source_id, True)
        self.assertTrue(self.store.dashboard()["next_actions"])
        self.store.delete_source(self.source_id)
        self.assertEqual([], self.store.sources())
        self.assertEqual([], self.store.list_sessions())
        self.assertEqual({}, self.store.get_cursor(self.source_id, "synthetic.jsonl"))
        with closing(sqlite3.connect(self.path)) as database:
            for table in ("events", "plans", "plan_versions", "steps"):
                self.assertEqual(0, database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def test_priority_due_and_snooze_rank_deterministically(self):
        event = self.event(payload={"steps": [
            {"id": "a", "description": "normal"},
            {"id": "b", "description": "high", "priority": 3},
            {"id": "c", "description": "overdue", "due_at": "2026-09-01"}]})
        self.ingest([event])
        board = self.store.dashboard(now="2026-09-16T00:00:00Z")
        self.assertEqual(["overdue", "high", "normal"], [row["description"] for row in board["next_actions"]])
        self.store.update_step(self.step("c")["id"], snoozed_until="2026-09-17T00:00:00Z")
        self.assertEqual(["high", "normal"], [row["description"] for row in self.store.dashboard(now="2026-09-16")["next_actions"]])
        self.assertEqual("overdue", self.store.dashboard(now="2026-09-18")["next_actions"][0]["description"])

    def test_pending_user_status_blocks_and_untrusted_plan_stays_suggested(self):
        self.ingest([self.event(payload={"steps": [{"description": "Waiting", "status": "awaiting_user"}]})])
        self.assertEqual(1, len(self.store.dashboard()["blocked"]))
        self.ingest([self.event("candidate", session_id="candidate", payload={
            "authoritative": False, "steps": [{"description": "Maybe"}]})])
        self.assertEqual(1, len(self.store.dashboard()["suggested"]))

    def test_settings_reminder_claims_persist_and_invalid_edits_reject(self):
        self.ingest([self.event()])
        step_id = self.step("a")["id"]
        self.assertFalse(self.store.get_setting("notifications", False))
        self.store.set_setting("notifications", True)
        self.assertTrue(self.store.get_setting("notifications"))
        self.assertTrue(self.store.claim_reminder(step_id, "due", "2026-09-16"))
        self.assertFalse(self.store.claim_reminder(step_id, "due", "2026-09-16"))
        with self.assertRaises(ValueError):
            self.store.update_step(step_id, status="made-up")
        with self.assertRaises(ValueError):
            self.store.update_step(step_id, priority=5)

    def test_environment_messages_do_not_become_session_title(self):
        self.ingest([self.event("context", "message", role="user", text="<environment_context>\nsecret context\n</environment_context>", payload={}),
                     self.event("request", "message", role="user", text="보고서 구현", payload={})])
        self.assertEqual("보고서 구현", self.store.list_sessions()[0]["title"])


if __name__ == "__main__":
    unittest.main()
