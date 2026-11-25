import argparse
import json
import sys
from . import __version__
from .client import CallError, Client
from .discover import discover, quick, summarize_capture
from .storage import load_json, save_private


def main():
    try:
        _main()
    except CallError as error:
        # Expected operational failures (blocked writes, missing credentials, bad
        # arguments…) are reported as structured JSON, never as a Python traceback.
        print(json.dumps(error.as_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(2) from None
    except (ValueError, OSError) as error:
        print(
            json.dumps({"error": str(error), "type": type(error).__name__}, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


def _main():
    parser = argparse.ArgumentParser(
        description="Browser observations → Python + MCP; no browser at call time"
    )
    parser.add_argument("--version", action="version", version=f"web2mcp {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    d = commands.add_parser("discover", help="Import ego capture or HAR; generate a local profile")
    d.add_argument("capture")
    d.add_argument("--out", required=True)
    d.add_argument("--origin", action="append", required=True)
    d.add_argument("--policy", help="Reviewed names/descriptions/read-only overrides")
    d.add_argument(
        "--read-only-safe",
        action="store_true",
        help="Produce a safe profile: expose only reads, hide and block all writes (read_only_mode)",
    )
    ins = commands.add_parser(
        "inspect", help="Offline overview of a capture/HAR: per-origin ops and a suggested discover command"
    )
    ins.add_argument("capture")
    r = commands.add_parser(
        "record",
        help="Open a Chromium window; log in and click around, close it to produce capture.json "
        "(needs: pip install 'web2mcp[record]')",
    )
    r.add_argument("url")
    r.add_argument("--out", required=True, help="Private directory for capture.json and browser profile")
    r.add_argument("--channel", help="Use an installed browser channel (e.g. chrome, msedge)")
    q = commands.add_parser(
        "quick",
        help="One command: capture/HAR -> auto-pick origin -> safe read-only MCP profile + paste-ready config",
    )
    q.add_argument("capture")
    q.add_argument("--out", help="Profile directory (default: <capture>-profile next to the file)")
    q.add_argument("--origin", action="append", help="Pin the API origin(s); otherwise auto-detected")
    ac = commands.add_parser(
        "add-curl",
        help="Read one 'Copy as cURL' command from stdin and add that operation to a profile",
    )
    ac.add_argument("profile")
    ac.add_argument(
        "--origin", action="append", help="Limit to these origins (default: inferred from the URL)"
    )
    ac.add_argument("--read-only-safe", action="store_true", help="Hide and block writes (read_only_mode)")
    io = commands.add_parser(
        "import-openapi",
        help="Build a profile from an OpenAPI 3.x JSON document (adds a write-safety layer to an existing API)",
    )
    io.add_argument("spec")
    io.add_argument("--out", required=True, help="Profile directory")
    io.add_argument("--origin", action="append", help="API origin(s) (default: the spec's servers URL)")
    io.add_argument("--writes", action="store_true", help="Expose writes too (default: read-only-safe)")
    ip = commands.add_parser(
        "import-postman", help="Build a profile from a Postman collection (v2.x JSON export)"
    )
    ip.add_argument("collection")
    ip.add_argument("--out", required=True, help="Profile directory")
    ip.add_argument("--origin", action="append", help="Limit to these origins (default: inferred)")
    ip.add_argument(
        "--var",
        action="append",
        default=[],
        help="Resolve a collection variable, e.g. --var baseUrl=https://api.example.com",
    )
    ip.add_argument("--writes", action="store_true", help="Expose writes too (default: read-only-safe)")
    lst = commands.add_parser("list")
    lst.add_argument("profile")
    c = commands.add_parser("call")
    c.add_argument("profile")
    c.add_argument("operation")
    c.add_argument("--args", default="{}")
    c.add_argument("--args-file", help="Load JSON arguments from a private local file")
    c.add_argument("--confirm", action="store_true", help="Send a reviewed write (otherwise preview)")
    s = commands.add_parser("serve")
    s.add_argument("profile")
    e = commands.add_parser("explore-start", help="Create a goal-driven browser exploration session")
    e.add_argument("directory")
    e.add_argument("--url", required=True)
    e.add_argument("--goal", required=True)
    e.add_argument("--origin", action="append")
    e.add_argument("--budget", type=int, default=40)
    e.add_argument(
        "--backend",
        choices=("ego", "playwright", "cdp"),
        help="Browser backend (default: auto — ego if installed, else playwright)",
    )
    e.add_argument(
        "--storage-sources",
        help="JSON file declaring exact origin/localStorage/sessionStorage keys; no values",
    )
    e = commands.add_parser("explore-round", help="Observe or execute bounded reviewed UI actions")
    e.add_argument("directory")
    e.add_argument("--action-file", required=True)
    e = commands.add_parser(
        "explore-survey",
        help="Autonomously follow in-scope links (GET only) to capture read endpoints across views",
    )
    e.add_argument("directory")
    e.add_argument("--max-pages", type=int, default=20)
    e = commands.add_parser(
        "source-map", help="Statically extract the API surface from the app's shipped JavaScript"
    )
    e.add_argument("directory")
    e = commands.add_parser(
        "synth-profile",
        help="Auto-build a serveable profile from the source map (reads trial-ready, writes preview)",
    )
    e.add_argument("directory")
    e.add_argument("--api-origin")
    e = commands.add_parser(
        "auto-publish", help="Auto-trial every read op and publish the ones that return data"
    )
    e.add_argument("directory")
    e.add_argument("--profile", default="synth")
    e = commands.add_parser("explore-propose", help="Generate candidate policy, tool cards and draft profile")
    e.add_argument("directory")
    e.add_argument("--policy")
    e.add_argument("--revision", action="store_true")
    e = commands.add_parser(
        "serve-explorer", help="Expose goal-driven browser exploration tools to a connected AI"
    )
    e.add_argument("directory")
    e = commands.add_parser("refresh-login")
    e.add_argument("profile")
    e.add_argument("capture")
    e = commands.add_parser("refresh-storage", help="Renew a pinned browser storage source after user login")
    e.add_argument("profile")
    e.add_argument("snapshot")
    e = commands.add_parser("install-contract")
    e.add_argument("profile")
    e.add_argument("contract")
    e.add_argument("--reviewed", action="store_true")
    e = commands.add_parser(
        "draft-read-cases",
        help="Draft trial-read cases (parameters + assertions) for each read op from the capture samples",
    )
    e.add_argument("profile")
    e.add_argument("capture")
    e = commands.add_parser("trial-read")
    e.add_argument("profile")
    e.add_argument("operation")
    e.add_argument("cases")
    e.add_argument("--reviewed-read-only", action="store_true")
    e = commands.add_parser("publish-reads")
    e.add_argument("profile")
    e.add_argument("names", nargs="+")
    e = commands.add_parser("purge-diagnostics")
    e.add_argument("profile")
    e.add_argument("--days", type=int, default=7)
    e = commands.add_parser("draft-pack", help="Draft a portable integration pack from observed operations")
    e.add_argument("profile")
    e.add_argument("--name", required=True)
    e.add_argument("--out", required=True)
    for command in ("check-pack", "trial-pack", "install-pack"):
        e = commands.add_parser(command)
        e.add_argument("profile")
        e.add_argument("pack")
        if command == "install-pack":
            e.add_argument("--reviewed", action="store_true")
    e = commands.add_parser(
        "serve-integrations", help="Run autonomous URL-to-MCP integration jobs through a local HTTP API"
    )
    e.add_argument("directory")
    e.add_argument("--model-config", required=True)
    e.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if args.command == "serve-integrations":
        from .integration_api import serve

        serve(args.directory, args.model_config, args.port)
        return
    if args.command == "serve-explorer":
        from .explorer_server import serve

        serve(args.directory)
        return
    elif args.command.startswith("explore-") or args.command in (
        "source-map",
        "synth-profile",
        "auto-publish",
    ):
        from .explore import start_session, explore_round, propose

        if args.command == "explore-start":
            result = start_session(
                args.directory,
                args.url,
                args.goal,
                args.origin,
                args.budget,
                load_json(args.storage_sources) if args.storage_sources else None,
                backend=args.backend,
            )
        elif args.command == "explore-round":
            result = explore_round(args.directory, load_json(args.action_file))
        elif args.command == "explore-survey":
            result = explore_round(args.directory, {"kind": "survey", "max_pages": args.max_pages})
        elif args.command == "source-map":
            from .explore import source_map

            result = source_map(args.directory)
        elif args.command == "synth-profile":
            from .explore import synthesize_profile

            result = synthesize_profile(args.directory, api_origin=args.api_origin)
        elif args.command == "auto-publish":
            from .explore import auto_publish_reads

            result = auto_publish_reads(args.directory, profile=args.profile)
        else:
            result = propose(
                args.directory, load_json(args.policy) if args.policy else None, revision=args.revision
            )
    elif args.command == "refresh-login":
        from .runtime import refresh_credentials

        result = refresh_credentials(args.profile, args.capture)
    elif args.command == "refresh-storage":
        from .runtime import refresh_storage_credentials

        result = refresh_storage_credentials(args.profile, args.snapshot)
    elif args.command == "purge-diagnostics":
        from .audit import purge_diagnostics

        result = purge_diagnostics(args.profile, args.days)
    elif args.command == "discover":
        result = discover(
            args.capture,
            args.out,
            args.origin,
            load_json(args.policy) if args.policy else None,
            read_only_safe=args.read_only_safe,
        )
    elif args.command == "inspect":
        result = summarize_capture(args.capture)
    elif args.command == "record":
        from .record import record

        result = record(args.url, args.out, browser_channel=args.channel)
    elif args.command == "quick":
        result = quick(args.capture, args.out, args.origin)
    elif args.command == "draft-read-cases":
        from .runtime import draft_read_cases

        result = draft_read_cases(args.profile, args.capture)
    elif args.command == "import-openapi":
        from .import_spec import import_openapi

        result = import_openapi(args.spec, args.out, origins=args.origin, read_only_safe=not args.writes)
    elif args.command == "import-postman":
        from .import_spec import import_postman

        variables = dict(pair.split("=", 1) for pair in args.var if "=" in pair)
        result = import_postman(
            args.collection,
            args.out,
            origins=args.origin,
            variables=variables,
            read_only_safe=not args.writes,
        )
    elif args.command == "add-curl":
        from .add_curl import add_curl

        result = add_curl(
            args.profile, sys.stdin.read(), origins=args.origin, read_only_safe=args.read_only_safe
        )
    elif args.command == "serve":
        from .server import serve

        serve(args.profile)
        return
    else:
        with Client(args.profile) as client:
            if args.command in ("draft-pack", "check-pack", "trial-pack", "install-pack"):
                from .packs import draft_pack, compile_pack, trial_pack, install_pack

                if args.command == "draft-pack":
                    save_private(args.out, draft_pack(client, args.name))
                    result = {"draft": args.out, "published": False}
                elif args.command == "check-pack":
                    pack = compile_pack(client, load_json(args.pack))
                    result = {"valid": True, "reads": len(pack["reads"]), "contracts": len(pack["contracts"])}
                elif args.command == "trial-pack":
                    result = trial_pack(client, load_json(args.pack))
                else:
                    result = install_pack(client, load_json(args.pack), reviewed=args.reviewed)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return
            from .packs import PackReader
            from .contracts import load_contract_engine, install_contract
            from .runtime import load_runtime, trial_read, publish_reads

            adapters = [
                adapter
                for adapter in (load_contract_engine(client), PackReader(client), load_runtime(client))
                if adapter
            ]
            if args.command == "install-contract":
                result = install_contract(client, load_json(args.contract), reviewed=args.reviewed)
            elif args.command == "trial-read":
                result = trial_read(
                    client, args.operation, load_json(args.cases), reviewed_read_only=args.reviewed_read_only
                )
            elif args.command == "publish-reads":
                result = publish_reads(client, args.names)
            elif args.command == "list":
                result = [op for op in client.describe() if not client.operations[op["name"]].get("internal")]
                result += [op for adapter in adapters for op in adapter.definitions()]
            else:
                arguments = load_json(args.args_file) if args.args_file else json.loads(args.args)
                adapter = next(
                    (a for a in adapters if args.operation in {x["name"] for x in a.definitions()}), None
                )
                if adapter:
                    if args.confirm:
                        arguments["confirm"] = True
                    result = adapter.call(args.operation, arguments)
                else:
                    result = client.call(args.operation, arguments, confirm=args.confirm)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
