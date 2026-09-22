#!/usr/bin/env python3
"""Engine contract parity — the doors a replacement engine must keep.

Two modes, one file, so the snapshot and the check can never drift apart:

    python3 scripts/contract-parity.py --snapshot   # write data/contract/engine-contract.json from the live engine
    python3 scripts/contract-parity.py --check      # compare the live engine against the committed snapshot

The snapshot holds three things a consumer (agents/, hooks/, cron, MCP clients) can see:
  * MCP tools — every `tools/list` entry, name + inputSchema (descriptions are prose, not contract)
  * HTTP routes — method + path, read from drudge/src/serve.rs (the router is the only place they exist)
  * GET shapes — the top-level keys of the read-only GET endpoints, taken live

Anything the check cannot read is a failure, not an empty set: an unreachable engine must go red,
otherwise "0 tools == 0 tools" passes for a dead server (CLAUDE.md §4).
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT = os.path.join(ROOT, "data", "contract", "engine-contract.json")
SERVE_RS = os.path.join(ROOT, "drudge", "src", "serve.rs")
# path → keys the response may omit: the `#[serde(skip_serializing_if = "Option::is_none")]` fields
# of the response struct (drudge/src/serve.rs `HealthResp`). A CI engine built without a git sha has
# no `build_sha`, an engine without a store has no `corpus_count`/`db_healthy`, `compact_failure`
# appears only after a failed compact. Every key not listed here is required.
GET_SHAPES = {
    "/health": ("build_sha", "compact_failure", "corpus_count", "db_healthy"),
    "/audit": (),
    "/projects": (),
    "/recall-label-stats": (),
}


def engine_url() -> str:
    return os.environ.get("DRUDGE_URL", "http://127.0.0.1:7700").rstrip("/")


def fetch_json(url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def live_tools(base: str) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    result = fetch_json(f"{base}/mcp", payload)["result"]["tools"]
    return {t["name"]: t["inputSchema"] for t in result}


def routes_from_source() -> list[str]:
    """`.route("/path", get(..).post(..))` → ["GET /path", "POST /path"]."""
    text = open(SERVE_RS, encoding="utf-8").read()
    out = set()
    for path, handlers in re.findall(r'\.route\("([^"]+)",\s*([^)]*\)(?:\.[a-z]+\([^)]*\))*)', text):
        for method in re.findall(r"\b(get|post|put|delete|patch)\(", handlers):
            out.add(f"{method.upper()} {path}")
    return sorted(out)


def live_shapes(base: str) -> dict:
    """{path: {"required": keys seen live minus the declared optional, "optional": declared, "seen": live keys}}."""
    out = {}
    for path, optional in GET_SHAPES.items():
        seen = set(fetch_json(f"{base}{path}").keys())
        out[path] = {
            "required": sorted(seen - set(optional)),
            "optional": sorted(optional),
            "seen": sorted(seen),
        }
    return out


def capture(base: str) -> dict:
    return {
        "mcp_tools": live_tools(base),
        "http_routes": routes_from_source(),
        "get_shapes": live_shapes(base),
    }


def diff(expected: dict, actual: dict) -> list[str]:
    lines: list[str] = []
    exp_tools, act_tools = expected["mcp_tools"], actual["mcp_tools"]
    for name in sorted(set(exp_tools) - set(act_tools)):
        lines.append(f"mcp tool missing: {name}")
    for name in sorted(set(act_tools) - set(exp_tools)):
        lines.append(f"mcp tool added (update the snapshot on purpose): {name}")
    for name in sorted(set(exp_tools) & set(act_tools)):
        if exp_tools[name] != act_tools[name]:
            lines.append(f"mcp tool schema changed: {name}")
    exp_routes, act_routes = set(expected["http_routes"]), set(actual["http_routes"])
    for r in sorted(exp_routes - act_routes):
        lines.append(f"http route missing: {r}")
    for r in sorted(act_routes - exp_routes):
        lines.append(f"http route added (update the snapshot on purpose): {r}")
    for path, shape in expected["get_shapes"].items():
        got = actual["get_shapes"].get(path)
        if got is None:
            lines.append(f"get shape missing: {path}")
            continue
        required, optional, live = set(shape["required"]), set(shape["optional"]), set(got["seen"])
        for key in sorted(required - live):
            lines.append(f"get shape key missing: {path} {key}")
        for key in sorted(live - required - optional):
            lines.append(f"get shape key added (update the snapshot on purpose): {path} {key}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--snapshot", action="store_true")
    mode.add_argument("--check", action="store_true")
    ap.add_argument("--file", default=SNAPSHOT)
    args = ap.parse_args()

    base = engine_url()
    try:
        actual = capture(base)
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        print(f"contract-parity: cannot read the live engine at {base}: {e}", file=sys.stderr)
        return 2

    if args.snapshot:
        os.makedirs(os.path.dirname(args.file), exist_ok=True)
        # `seen` is what this engine returned today; the contract is required + optional
        snapshot = dict(actual)
        snapshot["get_shapes"] = {
            p: {"required": s["required"], "optional": s["optional"]} for p, s in actual["get_shapes"].items()
        }
        with open(args.file, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        print(
            f"contract-parity: wrote {os.path.relpath(args.file, ROOT)} — "
            f"{len(actual['mcp_tools'])} tools, {len(actual['http_routes'])} routes, {len(actual['get_shapes'])} shapes"
        )
        return 0

    try:
        expected = json.load(open(args.file, encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"contract-parity: snapshot unreadable ({args.file}): {e}", file=sys.stderr)
        return 2
    lines = diff(expected, actual)
    if lines:
        print("contract-parity: FAIL")
        for line in lines:
            print(f"  - {line}")
        return 1
    print(
        f"contract-parity: ok — {len(expected['mcp_tools'])} tools, "
        f"{len(expected['http_routes'])} routes, {len(expected['get_shapes'])} shapes match the live engine"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
