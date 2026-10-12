import json
import sqlite3
import time
from datetime import datetime, timezone
from contextlib import contextmanager
from .delta import digest


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path="evidence.sqlite3"):
        self.path = str(path)
        with self.db() as c:
            c.executescript("""PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY,watch_key TEXT,body TEXT);
                CREATE TABLE IF NOT EXISTS ledger(seq INTEGER PRIMARY KEY,previous TEXT,hash TEXT,body TEXT);
                CREATE TABLE IF NOT EXISTS usage(day TEXT PRIMARY KEY,calls INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS snapshots_watch ON snapshots(watch_key,id);""")

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def reserve(self, calls, limit=24):
        day = utc()[:10]
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            used = c.execute("SELECT calls FROM usage WHERE day=?", (day,)).fetchone()
            used = used[0] if used else 0
            if used + calls > limit:
                raise ValueError(
                    f"Daily local budget exhausted: {used}/{limit} reserved calls"
                )
            c.execute(
                "INSERT INTO usage VALUES(?,?) ON CONFLICT(day) DO UPDATE SET calls=excluded.calls",
                (day, used + calls),
            )
        return used + calls

    def usage(self):
        with self.db() as c:
            r = c.execute(
                "SELECT calls FROM usage WHERE day=?", (utc()[:10],)
            ).fetchone()
            return r[0] if r else 0

    def append(self, watch, payloads, mode="live", complete=True, trace=None):
        key = digest(watch)
        body = {
            "watch": watch,
            "watch_key": key,
            "payloads": payloads,
            "mode": mode,
            "complete": complete,
            "fetched_at": utc(),
            "epoch": time.time(),
            "trace": trace or [],
        }
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(
                "INSERT INTO snapshots(watch_key,body) VALUES(?,?)", (key, "{}")
            )
            body["id"] = cur.lastrowid
            raw = json.dumps(
                body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            c.execute("UPDATE snapshots SET body=? WHERE id=?", (raw, body["id"]))
            last = c.execute(
                "SELECT hash FROM ledger ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            previous = last[0] if last else "0" * 64
            event = {
                "snapshot_id": body["id"],
                "snapshot_hash": digest(body),
                "watch_key": key,
                "at": body["fetched_at"],
            }
            event_raw = json.dumps(event, sort_keys=True, separators=(",", ":"))
            chain = digest({"previous": previous, "event": event})
            c.execute(
                "INSERT INTO ledger(previous,hash,body) VALUES(?,?,?)",
                (previous, chain, event_raw),
            )
        return body

    def get(self, snapshot_id):
        with self.db() as c:
            r = c.execute(
                "SELECT body FROM snapshots WHERE id=?", (snapshot_id,)
            ).fetchone()
        if not r:
            raise ValueError("Unknown snapshot")
        return json.loads(r[0])

    def history(self):
        with self.db() as c:
            rows = c.execute(
                "SELECT body FROM snapshots ORDER BY id DESC LIMIT 100"
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def latest(self, watch, mode="live"):
        key = digest(watch)
        with self.db() as c:
            rows = c.execute(
                "SELECT body FROM snapshots WHERE watch_key=? ORDER BY id DESC", (key,)
            ).fetchall()
        return next(
            (
                s
                for s in (json.loads(r[0]) for r in rows)
                if s["mode"] == mode and s["complete"]
            ),
            None,
        )

    def verify(self):
        return verify_bundle(self.export(False))

    def export(self, verification=True):
        with self.db() as c:
            ledger = [dict(r) for r in c.execute("SELECT * FROM ledger ORDER BY seq")]
            snapshots = []
            for r in c.execute("SELECT id,body FROM snapshots ORDER BY id"):
                try:
                    snapshots.append(json.loads(r["body"]))
                except json.JSONDecodeError:
                    snapshots.append({"id": r["id"], "malformed": True})
        bundle = {
            "format": "EvidenceDelta bundle v1",
            "snapshots": snapshots,
            "ledger": ledger,
        }
        if verification:
            bundle["verification"] = verify_bundle(bundle)
        return bundle


def verify_bundle(bundle):
    previous = "0" * 64
    failures = []
    indexed = []
    snapshots = {s["id"]: s for s in bundle["snapshots"]}
    rows = bundle["ledger"]
    for seq, r in enumerate(rows, 1):
        try:
            event = json.loads(r["body"])
            indexed.append(event["snapshot_id"])
            snapshot = snapshots[event["snapshot_id"]]
            if digest(snapshot) != event["snapshot_hash"]:
                failures.append({"seq": seq, "reason": "snapshot content changed"})
            if (
                r["seq"] != seq
                or r["previous"] != previous
                or digest({"previous": r["previous"], "event": event}) != r["hash"]
            ):
                failures.append({"seq": seq, "reason": "ledger link changed"})
            previous = r["hash"]
        except (ValueError, KeyError, TypeError):
            failures.append({"seq": seq, "reason": "missing or malformed record"})
    if (
        set(snapshots) != set(indexed)
        or len(indexed) != len(set(indexed))
        or len(snapshots) != len(bundle["snapshots"])
    ):
        failures.append({"reason": "snapshot/ledger index mismatch"})
    return {
        "valid": not failures,
        "entries": len(rows),
        "head": previous,
        "failures": failures,
        "limitation": "Unanchored local chain: detects edits against this history, but not a complete rewrite or deletion of a valid suffix. Save the head hash independently.",
    }
