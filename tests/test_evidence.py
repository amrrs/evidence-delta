import copy
import itertools
import json
import random
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from evidence_delta.delta import (
    canonical_url,
    exact_assignment,
    rank_overlap,
    compare_engine,
    compare,
)
from evidence_delta.service import Service, watch_config, redact, ci_gate
from evidence_delta.store import Store, verify_bundle


def payload():
    return {
        "organic_results": [
            {
                "title": f"Unique source {i}",
                "link": f"https://site{i}.example/a?id={i}",
                "snippet": f"Evidence {i}",
            }
            for i in range(6)
        ],
        "news_results": [
            {
                "title": f"Unique source {i}",
                "link": f"https://site{i}.example/a?id={i}",
                "snippet": f"Evidence {i}",
            }
            for i in range(6)
        ],
    }


class EngineTests(unittest.TestCase):
    def test_url_tracking_removed_semantics_retained(self):
        self.assertEqual(
            canonical_url("http://WWW.Example.com/a/?b=2&utm_source=x&a=1#x"),
            "https://example.com/a?a=1&b=2",
        )
        self.assertNotEqual(
            canonical_url("https://example.com/?id=1"),
            canonical_url("https://example.com/?id=2"),
        )
        with self.assertRaises(ValueError):
            canonical_url("javascript:alert(1)")

    def test_assignment_beats_greedy(self):
        self.assertEqual(exact_assignment([[0.9, 0.8], [0.85, 0]])["score"], 1.65)

    def test_assignment_against_independent_bruteforce(self):
        rng = random.Random(23)
        for n in range(1, 5):
            for m in range(1, 5):
                matrix = [
                    [rng.choice([0, 0.3, 0.6, 1]) for _ in range(m)] for _ in range(n)
                ]
                optimum = max(
                    sum(matrix[i][j] for i, j in enumerate(js) if j >= 0)
                    for js in itertools.product(range(-1, m), repeat=n)
                    if len([j for j in js if j >= 0])
                    == len(set(j for j in js if j >= 0))
                )
                self.assertAlmostEqual(exact_assignment(matrix)["score"], optimum)

    def test_bound_and_ragged_rejected(self):
        for x in ([[1] * 11], [[1], [1, 2]]):
            with self.assertRaises(ValueError):
                exact_assignment(x)

    def test_rbo_identity_disjoint_and_top_weight(self):
        a = list("abcde")
        self.assertEqual(rank_overlap(a, a)["value"], 1)
        self.assertEqual(rank_overlap(a, list("vwxyz"))["value"], 0)
        self.assertLess(
            rank_overlap(a, list("bacde"))["value"],
            rank_overlap(a, list("abced"))["value"],
        )
        self.assertIsNone(rank_overlap([], a)["value"])

    def test_rank_is_separate_from_content(self):
        a = payload()
        b = copy.deepcopy(a)
        b["organic_results"][0:2] = reversed(b["organic_results"][0:2])
        r = compare_engine(a, b, "google")
        self.assertEqual(r["counts"]["rank_only"], 2)
        self.assertEqual(r["counts"]["content"], 0)
        b["organic_results"][0]["snippet"] = "Changed evidence"
        r = compare_engine(a, b, "google")
        self.assertEqual(r["counts"]["content"], 1)
        self.assertEqual(r["counts"]["rank_only"], 1)

    def test_alias_explicit_and_strict_optional(self):
        a = payload()
        b = copy.deepcopy(a)
        b["organic_results"][0]["link"] = "https://site0.example/new"
        r = compare_engine(a, b, "google")
        self.assertEqual(r["counts"]["url_alias"], 1)
        self.assertEqual(
            r["changes"][0]["match"]["method"], "same-host title hypothesis"
        )
        r = compare_engine(a, b, "google", False)
        self.assertEqual(r["counts"]["removed"], 1)
        self.assertEqual(r["counts"]["added"], 1)

    def test_different_hosts_never_fuzzy_match(self):
        a = payload()
        b = copy.deepcopy(a)
        b["organic_results"][0]["link"] = "https://elsewhere.example/new"
        self.assertEqual(compare_engine(a, b, "google")["counts"]["removed"], 1)

    def test_redaction_recursive(self):
        self.assertEqual(
            redact(
                {
                    "api_key": "SECRET",
                    "nested": ["xSECRET", {"authorization": "SECRET"}],
                },
                "SECRET",
            ),
            {"nested": ["x[REDACTED]", {}]},
        )


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "state.db")
        self.store = Store(self.path)
        self.watch = watch_config("test query")
        self.calls = []

        def fake(w, e, k, f):
            self.calls.append(e)
            return payload(), {"engine": e, "search_id": "test fixture"}

        self.service = Service(self.store, fake, "test-key", ttl=60)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ttl_reuses_without_new_snapshot_or_calls(self):
        a = self.service.capture(self.watch)
        b = self.service.capture(self.watch)
        self.assertEqual(a["new_calls"], 2)
        self.assertEqual(b["new_calls"], 0)
        self.assertTrue(b["cached"])
        self.assertEqual(len(self.store.history()), 1)
        self.assertEqual(len(self.calls), 2)

    def test_failed_observation_excluded_and_charged(self):
        def fail(*args):
            raise ValueError("fixture failure")

        s = Service(self.store, fail, "key")
        r = s.capture(self.watch)
        self.assertFalse(r["snapshot"]["complete"])
        self.assertIsNone(self.store.latest(self.watch))
        self.assertEqual(self.store.usage(), 2)
        with self.assertRaises(ValueError):
            compare(r["snapshot"], r["snapshot"])

    def test_budget_atomic_across_threads(self):
        def reserve(_):
            try:
                self.store.reserve(2, 6)
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(reserve, range(10)))
        self.assertEqual(sum(results), 3)
        self.assertEqual(self.store.usage(), 6)

    def test_live_and_controlled_modes_are_distinct(self):
        live = self.service.capture(self.watch)["snapshot"]
        r = self.service.replay(live["id"])
        self.assertEqual(r["new_calls"], 0)
        self.assertEqual(self.store.usage(), 2)
        self.assertEqual(r["after"]["mode"], "controlled replay")
        self.assertEqual(self.store.latest(self.watch)["id"], live["id"])
        for e in r["comparison"]["engines"]:
            self.assertEqual(e["counts"]["content"], 1)
            self.assertEqual(e["counts"]["added"], 1)
            self.assertEqual(e["counts"]["removed"], 1)
            self.assertEqual(e["counts"]["url_alias"], 1)
        self.assertFalse(ci_gate(r["comparison"])["passed"])
        with self.assertRaises(ValueError):
            self.service.replay(r["after"]["id"])

    def test_replay_changes_stay_inside_normalized_window(self):
        large = payload()
        large["news_results"] *= 3
        large["news_results"] += [
            {"title": f"Extra {i}", "link": f"https://extra{i}.example/", "snippet": ""}
            for i in range(20)
        ]
        s = self.store.append(self.watch, {"google": large, "google_news": large})
        r = self.service.replay(s["id"])
        for e in r["comparison"]["engines"]:
            self.assertEqual(e["counts"]["added"], 1)
            self.assertEqual(e["counts"]["removed"], 1)

    def test_snapshot_edit_detected(self):
        s = self.service.capture(self.watch)["snapshot"]
        self.assertTrue(self.store.verify()["valid"])
        s["payloads"]["google"]["organic_results"][0]["snippet"] = "tampered"
        with self.store.db() as c:
            c.execute(
                "UPDATE snapshots SET body=? WHERE id=?", (json.dumps(s), s["id"])
            )
        self.assertFalse(self.store.verify()["valid"])

    def test_malformed_ledger_detected_without_crash(self):
        self.service.capture(self.watch)
        with self.store.db() as c:
            c.execute("UPDATE ledger SET body='broken'")
        self.assertFalse(self.store.verify()["valid"])

    def test_offline_bundle_and_full_export(self):
        for i in range(102):
            self.store.append(
                self.watch, {"google": payload(), "google_news": payload()}
            )
        bundle = self.store.export()
        self.assertEqual(len(bundle["snapshots"]), 102)
        self.assertTrue(verify_bundle(bundle)["valid"])
        bundle["ledger"][0]["previous"] = "changed"
        self.assertFalse(verify_bundle(bundle)["valid"])

    def test_cli_gate_exit_status(self):
        live = self.service.capture(self.watch)["snapshot"]
        r = self.service.replay(live["id"])
        command = [
            sys.executable,
            "-m",
            "evidence_delta",
            "--db",
            self.path,
            "compare",
            str(r["before"]["id"]),
            str(r["after"]["id"]),
            "--gate",
        ]
        failed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(failed.returncode, 1)
        self.assertFalse(json.loads(failed.stdout)["gate"]["passed"])
        passed = subprocess.run(
            command + ["--max-content", "1", "--max-removed", "1"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(passed.returncode, 0)

    def test_negative_gate_rejected(self):
        with self.assertRaises(ValueError):
            ci_gate({"engines": []}, max_content=-1)

    def test_offline_cli_reproduces_report_and_rejects_tampering(self):
        live = self.service.capture(self.watch)["snapshot"]
        r = self.service.replay(live["id"])
        bundle = self.store.export()
        path = Path(self.tmp.name) / "bundle.json"
        path.write_text(json.dumps(bundle))
        command = [
            sys.executable,
            "-m",
            "evidence_delta",
            "--db",
            self.path,
            "compare",
            str(r["before"]["id"]),
            str(r["after"]["id"]),
            "--bundle",
            str(path),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), r["comparison"])
        bundle["snapshots"][0]["mode"] = "tampered"
        path.write_text(json.dumps(bundle))
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
