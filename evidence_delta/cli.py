import argparse
import json
from .delta import compare
from .service import Service, watch_config, load_env, ci_gate
from .store import Store, verify_bundle
from pathlib import Path


def main():
    p = argparse.ArgumentParser(
        description="Version and compare SerpApi evidence for developer workflows"
    )
    p.add_argument("--db", default="evidence.sqlite3")
    s = p.add_subparsers(dest="command", required=True)
    s.add_parser("history")
    v = s.add_parser("verify")
    v.add_argument("--bundle")
    s.add_parser("export")
    c = s.add_parser("capture")
    c.add_argument("query")
    c.add_argument("--force", action="store_true")
    c.add_argument("--gl", default="in")
    c.add_argument("--hl", default="en")
    r = s.add_parser("replay")
    r.add_argument("--source", type=int)
    c = s.add_parser("compare")
    c.add_argument("before", type=int)
    c.add_argument("after", type=int)
    c.add_argument("--strict", action="store_true")
    c.add_argument("--p", type=float, default=0.9)
    c.add_argument("--gate", action="store_true")
    c.add_argument("--max-content", type=int, default=0)
    c.add_argument("--max-removed", type=int, default=0)
    c.add_argument("--min-rbo", type=float, default=0)
    c.add_argument(
        "--bundle",
        help="Compare snapshot IDs from an exported bundle, without an API key",
    )
    c = s.add_parser("serve")
    c.add_argument("--port", type=int, default=8767)
    args = p.parse_args()
    load_env()
    store = Store(args.db)
    service = Service(store)
    code = 0
    try:
        if args.command == "capture":
            out = service.capture(
                watch_config(args.query, gl=args.gl, hl=args.hl), args.force
            )
            code = 0 if out["snapshot"]["complete"] else 2
        elif args.command == "history":
            out = store.history()
        elif args.command == "verify":
            out = (
                verify_bundle(json.loads(Path(args.bundle).read_text()))
                if args.bundle
                else store.verify()
            )
            code = 0 if out["valid"] else 2
        elif args.command == "export":
            out = store.export()
        elif args.command == "replay":
            out = service.replay(args.source)
        elif args.command == "compare":
            if args.bundle:
                bundle = json.loads(Path(args.bundle).read_text())
                if not verify_bundle(bundle)["valid"]:
                    raise ValueError("Bundle integrity verification failed")
                lookup = {x["id"]: x for x in bundle["snapshots"]}
                before, after = lookup[args.before], lookup[args.after]
            else:
                before, after = store.get(args.before), store.get(args.after)
            out = compare(before, after, not args.strict, args.p)
            if args.gate:
                out["gate"] = ci_gate(
                    out, args.max_content, args.max_removed, args.min_rbo
                )
                code = 0 if out["gate"]["passed"] else 1
        else:
            from .server import serve

            serve(args.port, service)
            return
        print(json.dumps(out, indent=2, ensure_ascii=False))
    except (ValueError, KeyError, TypeError, OSError) as e:
        print(json.dumps({"error": str(e)}))
        code = 2
    raise SystemExit(code)
