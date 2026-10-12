"""Deterministic search-evidence comparison; no LLM or article truth inference."""

import difflib
import hashlib
import json
import re
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

VERSION = "evidence-delta/1"


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def canonical_url(url):
    u = urlsplit(url)
    if u.scheme.lower() not in ("http", "https") or not u.hostname:
        raise ValueError("Only HTTP(S) source URLs are accepted")
    host = u.hostname.lower().removeprefix("www.")
    port = u.port
    if port and port not in (80, 443):
        host += ":" + str(port)
    # Preserve semantic query parameters; remove only known tracking parameters.
    query = sorted(
        (k, v)
        for k, v in parse_qsl(u.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
        and k.lower() not in ("gclid", "fbclid", "msclkid")
    )
    path = u.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


def words(text):
    return set(re.findall(r"\w+", text.casefold()))


def jaccard(a, b):
    a, b = words(a), words(b)
    return len(a & b) / len(a | b) if a | b else 0.0


def normalize(payload, engine):
    field = "news_results" if engine == "google_news" else "organic_results"
    out, seen = [], set()
    for row in payload.get(field, []):
        try:
            canonical = canonical_url(row.get("link", ""))
        except (ValueError, TypeError):
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        title = str(row.get("title") or "").strip()
        snippet = str(row.get("snippet") or "").strip()
        out.append(
            {
                "id": digest(canonical)[:16],
                "canonical": canonical,
                "url": row["link"],
                "title": title,
                "snippet": snippet,
                "rank": len(out) + 1,
                "engine": engine,
                "host": urlsplit(canonical).hostname,
                "fingerprint": digest(
                    " ".join((title + " " + snippet).casefold().split())
                ),
            }
        )
        if len(out) == 10:
            break
    return out


def exact_assignment(matrix):
    """Maximum-weight partial matching, O(n*m*2^m), bounded to <=10x10."""
    n, m = len(matrix), len(matrix[0]) if matrix else 0
    if n > 10 or m > 10 or any(len(row) != m for row in matrix):
        raise ValueError("Assignment is bounded to rectangular matrices up to 10x10")

    @lru_cache(None)
    def solve(i, used):
        if i == n:
            return (0.0, ())
        best, pairs = solve(i + 1, used)
        for j in range(m):
            if used & (1 << j) or matrix[i][j] <= 0:
                continue
            score, rest = solve(i + 1, used | (1 << j))
            score += matrix[i][j]
            if score > best + 1e-12:
                best, pairs = score, ((i, j),) + rest
        return best, pairs

    score, pairs = solve(0, 0)
    return {
        "score": round(score, 6),
        "pairs": list(pairs),
        "states": solve.cache_info().currsize,
    }


def align(before, after, fuzzy=True):
    matches, used_a, used_b = [], set(), set()
    for i, a in enumerate(before):
        for j, b in enumerate(after):
            if a["canonical"] == b["canonical"]:
                matches.append(
                    {"before": i, "after": j, "method": "canonical URL", "score": 1.0}
                )
                used_a.add(i)
                used_b.add(j)
                break
    aa = [i for i in range(len(before)) if i not in used_a]
    bb = [j for j in range(len(after)) if j not in used_b]
    matrix = []
    for i in aa:
        scores = []
        for j in bb:
            a, b = before[i], after[j]
            similarity = jaccard(a["title"], b["title"])
            # Fuzzy identity is an explicit hypothesis, restricted to the same host.
            scores.append(
                similarity
                if fuzzy and a["host"] == b["host"] and similarity >= 0.65
                else 0
            )
        matrix.append(scores)
    solution = exact_assignment(matrix)
    for i, j in solution["pairs"]:
        matches.append(
            {
                "before": aa[i],
                "after": bb[j],
                "method": "same-host title hypothesis",
                "score": matrix[i][j],
            }
        )
        used_a.add(aa[i])
        used_b.add(bb[j])
    return {
        "matches": matches,
        "added": [j for j in range(len(after)) if j not in used_b],
        "removed": [i for i in range(len(before)) if i not in used_a],
        "assignment": {
            "objective": "maximum total eligible title Jaccard; unmatched allowed",
            "score": solution["score"],
            "states": solution["states"],
            "matrix": matrix,
            "before_indices": aa,
            "after_indices": bb,
            "threshold": 0.65,
            "fuzzy_enabled": fuzzy,
        },
    }


def rank_overlap(a, b, persistence=0.9):
    """Equal-depth extrapolated RBO, evaluated only at shared depth."""
    if not 0 < persistence < 1:
        raise ValueError("Persistence must be between 0 and 1")
    k = min(len(a), len(b))
    if not k:
        return {
            "value": None,
            "depth": 0,
            "p": persistence,
            "agreements": [],
            "reason": "No shared depth",
        }
    overlaps = [len(set(a[:d]) & set(b[:d])) / d for d in range(1, k + 1)]
    value = (1 - persistence) * sum(
        x * persistence**i for i, x in enumerate(overlaps)
    ) + overlaps[-1] * persistence**k
    return {
        "value": round(value, 6),
        "depth": k,
        "p": persistence,
        "agreements": overlaps,
        "unexamined_tail_weight": round(persistence**k, 6),
        "reason": "Equal-depth extrapolation at shared depth; unequal-length tails excluded",
    }


def compare_engine(before, after, engine, fuzzy=True, persistence=0.9):
    a, b = normalize(before, engine), normalize(after, engine)
    matching = align(a, b, fuzzy)
    identities = {x["after"]: a[x["before"]]["id"] for x in matching["matches"]}
    ranks = rank_overlap(
        [x["id"] for x in a],
        [identities.get(j, x["id"]) for j, x in enumerate(b)],
        persistence,
    )
    changes = []
    counts = {
        "added": len(matching["added"]),
        "removed": len(matching["removed"]),
        "content": 0,
        "rank_only": 0,
        "url_alias": 0,
        "unchanged": 0,
    }
    for x in matching["matches"]:
        old, new = a[x["before"]], b[x["after"]]
        content = old["fingerprint"] != new["fingerprint"]
        moved = old["rank"] != new["rank"]
        alias = old["canonical"] != new["canonical"]
        kind = (
            "content"
            if content
            else ("url_alias" if alias else ("rank_only" if moved else "unchanged"))
        )
        counts[kind] += 1
        if kind != "unchanged":
            changes.append(
                {
                    "kind": kind,
                    "before": old,
                    "after": new,
                    "rank_moved": moved,
                    "match": x,
                    "diff": list(
                        difflib.ndiff(
                            (old["title"] + "\n" + old["snippet"]).splitlines(),
                            (new["title"] + "\n" + new["snippet"]).splitlines(),
                        )
                    ),
                }
            )
    changes += [{"kind": "added", "after": b[j]} for j in matching["added"]]
    changes += [{"kind": "removed", "before": a[i]} for i in matching["removed"]]
    domains = sorted(set(x["host"] for x in a + b))
    domain_flows = [
        {
            "domain": d,
            "before": sum(x["host"] == d for x in a),
            "after": sum(x["host"] == d for x in b),
        }
        for d in domains
    ]
    return {
        "engine": engine,
        "before_count": len(a),
        "after_count": len(b),
        "counts": counts,
        "rbo": ranks,
        "changes": changes,
        "alignment": matching,
        "domain_flows": domain_flows,
        "scope": "Changes to search-returned titles/snippets and rankings, not verified article edits or truth.",
    }


def compare(before, after, fuzzy=True, persistence=0.9):
    if before["watch"] != after["watch"]:
        raise ValueError(
            "Snapshots must have identical query, locale and engine settings"
        )
    if not before.get("complete") or not after.get("complete"):
        raise ValueError("Incomplete observations cannot form a comparison baseline")
    return {
        "version": VERSION,
        "before_id": before["id"],
        "after_id": after["id"],
        "before_at": before["fetched_at"],
        "after_at": after["fetched_at"],
        "watch": before["watch"],
        "provenance": {
            "before": before["mode"],
            "after": after["mode"],
            "before_trace": before.get("trace", []),
            "after_trace": after.get("trace", []),
        },
        "engines": [
            compare_engine(
                before["payloads"][e], after["payloads"][e], e, fuzzy, persistence
            )
            for e in before["watch"]["engines"]
        ],
        "warning": "Fuzzy matches are hypotheses. Disable them to use canonical URL identity only.",
    }
