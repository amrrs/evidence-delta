import copy
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from .delta import compare, normalize
from .store import Store


def load_env(path=".env"):
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.removeprefix("export ").split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def redact(value, secret=""):
    if isinstance(value, dict):
        return {
            k: redact(v, secret)
            for k, v in value.items()
            if k.lower() not in ("api_key", "authorization", "serpapi_api_key")
        }
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    if isinstance(value, str) and secret:
        return value.replace(secret, "[REDACTED]")
    return value


def watch_config(query, engines=None, gl="in", hl="en"):
    query = str(query).strip()
    if not query or len(query) > 240:
        raise ValueError("Query must contain 1–240 characters")
    engines = sorted(set(engines or ["google", "google_news"]))
    if not engines or any(e not in ("google", "google_news") for e in engines):
        raise ValueError("Unsupported engine")
    if not gl.isalpha() or len(gl) != 2 or not hl.isalpha() or not 2 <= len(hl) <= 5:
        raise ValueError("Invalid locale")
    return {
        "query": query,
        "engines": engines,
        "gl": gl.lower(),
        "hl": hl.lower(),
        "depth": 10,
    }


def fetch(watch, engine, key, force=False):
    params = {
        "q": watch["query"],
        "engine": engine,
        "gl": watch["gl"],
        "hl": watch["hl"],
        "api_key": key,
    }
    if engine == "google":
        params["num"] = 10
    if force:
        params["no_cache"] = "true"
    request = urllib.request.Request(
        "https://serpapi.com/search.json?" + urllib.parse.urlencode(params),
        headers={"User-Agent": "EvidenceDelta/0.1"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=40) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        raise ValueError(f"{engine} capture failed: HTTP {e.code}") from None
    except Exception:
        raise ValueError(
            f"{engine} capture failed: network or response error"
        ) from None
    if not isinstance(body, dict) or body.get("error"):
        raise ValueError(f"{engine} did not return a successful observation")
    metadata = body.get("search_metadata", {})
    if metadata.get("status") != "Success":
        raise ValueError(f"{engine} observation is incomplete")
    return redact(body, key), {
        "engine": engine,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "search_id": metadata.get("id"),
        "provider_created_at": metadata.get("created_at"),
        "provider_processed_at": metadata.get("processed_at"),
        "force_provider_fresh": force,
    }


class Service:
    def __init__(self, store=None, fetcher=fetch, key=None, limit=24, ttl=60):
        self.store = store or Store(os.getenv("EVIDENCE_DB", "evidence.sqlite3"))
        self.fetcher = fetcher
        self.key = key if key is not None else os.getenv("SERPAPI_API_KEY", "")
        self.limit = limit
        self.ttl = ttl
        self.lock = threading.Lock()

    def capture(self, watch, force=False):
        with self.lock:
            baseline = self.store.latest(watch)
            if baseline and not force and time.time() - baseline["epoch"] < self.ttl:
                return {
                    "snapshot": baseline,
                    "cached": True,
                    "new_calls": 0,
                    "comparison": None,
                    "message": "Local TTL reuse; no new observation was created",
                }
            if not self.key:
                raise ValueError(
                    "Set SERPAPI_API_KEY in the server environment or .env"
                )
            self.store.reserve(len(watch["engines"]), self.limit)
            payloads, trace, errors = {}, [], []
            with ThreadPoolExecutor(max_workers=2) as executor:
                jobs = {
                    e: executor.submit(self.fetcher, watch, e, self.key, force)
                    for e in watch["engines"]
                }
                for e, f in jobs.items():
                    try:
                        payload, t = f.result()
                        payloads[e] = payload
                        trace.append(t)
                    except ValueError as ex:
                        errors.append(str(ex))
            complete = not errors
            snapshot = self.store.append(watch, payloads, "live", complete, trace)
            return {
                "snapshot": snapshot,
                "cached": False,
                "new_calls": len(watch["engines"]),
                "comparison": compare(baseline, snapshot)
                if baseline and complete
                else None,
                "errors": errors,
                "message": "Incomplete capture is retained for diagnostics and excluded from baselines"
                if errors
                else "Live observation stored",
            }

    def replay(self, snapshot_id=None):
        source = (
            self.store.get(snapshot_id)
            if snapshot_id
            else next(
                (
                    s
                    for s in self.store.history()
                    if s["mode"] == "live" and s["complete"]
                ),
                None,
            )
        )
        if not source or source["mode"] != "live" or not source["complete"]:
            raise ValueError(
                "Capture a complete live observation before running a controlled replay"
            )
        watch = copy.deepcopy(source["watch"])
        watch["scenario"] = "controlled mutation from snapshot " + str(source["id"])
        before = self.store.append(
            watch, copy.deepcopy(source["payloads"]), "controlled replay"
        )
        payloads = copy.deepcopy(source["payloads"])
        mutations = []
        for engine, payload in payloads.items():
            field = "news_results" if engine == "google_news" else "organic_results"
            # Inject inside the same valid, deduplicated top-10 window the detector uses.
            rows = [
                {"title": r["title"], "link": r["url"], "snippet": r["snippet"]}
                for r in normalize(payload, engine)
            ]
            payload[field] = rows
            if len(rows) >= 2:
                rows[0], rows[1] = rows[1], rows[0]
                mutations.append(engine + ": swap first two results")
            if rows:
                rows[0]["snippet"] = (
                    str(rows[0].get("snippet", ""))
                    + " [CONTROLLED TEST: evidence revised]"
                )
                mutations.append(engine + ": inject labeled snippet mutation")
            if len(rows) > 3:
                u = urllib.parse.urlsplit(rows[2]["link"])
                q = urllib.parse.parse_qsl(u.query, keep_blank_values=True) + [
                    ("utm_source", "controlled_test")
                ]
                rows[2]["link"] = urllib.parse.urlunsplit(
                    u._replace(query=urllib.parse.urlencode(q))
                )
                mutations.append(engine + ": add tracking-only URL parameter")
                alias = urllib.parse.urlsplit(rows[3]["link"])
                rows[3]["link"] = urllib.parse.urlunsplit(
                    alias._replace(path=alias.path.rstrip("/") + "/controlled-alias")
                )
                mutations.append(
                    engine
                    + ": inject same-host URL alias hypothesis with unchanged title"
                )
            if len(rows) > 4:
                removed = rows.pop()
                mutations.append(
                    engine + ": remove final source " + str(removed.get("title", ""))
                )
                rows.append(
                    {
                        "title": "Controlled replay source — not a live finding",
                        "link": "https://example.invalid/controlled-evidence-delta",
                        "snippet": "Synthetic source for detector verification only.",
                    }
                )
                mutations.append(engine + ": insert synthetic source")
        after = self.store.append(watch, payloads, "controlled replay")
        return {
            "source_live_id": source["id"],
            "before": before,
            "after": after,
            "mutations": mutations,
            "comparison": compare(before, after),
            "new_calls": 0,
            "disclosure": "Controlled counterfactual based on a live snapshot. These edits are test injections, not historical events.",
        }


def ci_gate(report, max_content=0, max_removed=0, min_rbo=0):
    if max_content < 0 or max_removed < 0 or not 0 <= min_rbo <= 1:
        raise ValueError("Gate limits must be nonnegative; minimum RBO must be 0–1")
    failures = []
    for e in report["engines"]:
        if e["counts"]["content"] > max_content:
            failures.append(f"{e['engine']}: changed evidence exceeds {max_content}")
        if e["counts"]["removed"] > max_removed:
            failures.append(f"{e['engine']}: missing sources exceed {max_removed}")
        if e["rbo"]["value"] is None or e["rbo"]["value"] < min_rbo:
            failures.append(
                f"{e['engine']}: ranking overlap is unavailable or below {min_rbo}"
            )
    return {
        "passed": not failures,
        "failures": failures,
        "policy": {
            "max_content": max_content,
            "max_removed": max_removed,
            "min_rbo": min_rbo,
        },
    }
