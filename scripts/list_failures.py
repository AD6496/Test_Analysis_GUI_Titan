#!/usr/bin/env python3
"""List failed tests for a TestRail run, plan, or single test URL/id.

Usage:
    uv run python list_failures.py <url-or-run_id>
    uv run python list_failures.py --plan <plan_id>
    uv run python list_failures.py --run <run_id>
    uv run python list_failures.py --test <test_id>

Accepts a raw titan.zebra.lan TestRail URL (.../runs/view/N, .../plans/view/N,
.../tests/view/N) and dispatches automatically -- no manual URL parsing needed.
"""
import os
import re
import sys
import requests

_TESTRAIL_URL = os.environ.get("TESTRAIL_URL", "http://titan.zebra.lan/testrail/")
BASE = _TESTRAIL_URL.rstrip("/") + "/index.php?/api/v2"
AUTH = (os.environ.get("TESTRAIL_USER", ""), os.environ.get("TESTRAIL_PASSWORD", ""))


def get(ep):
    r = requests.get(f"{BASE}/{ep}", auth=AUTH, timeout=30)
    r.raise_for_status()
    return r.json()


def list_failed(run_id, prefix="", status_ids="5"):
    data = get(f"get_tests/{run_id}&status_id={status_ids}")
    tests = data.get("tests", data if isinstance(data, list) else [])
    tests.sort(key=lambda t: t["id"])
    for t in tests:
        print(f"{t['id']}\t{prefix}{t['title']}")
    return tests


def do_run(run_id):
    tests = list_failed(run_id)
    print(f"count {len(tests)}")


def do_plan(plan_id):
    plan = get(f"get_plan/{plan_id}")
    runs = [run for entry in plan.get("entries", []) for run in entry.get("runs", [])]
    total = 0
    for run in sorted(runs, key=lambda r: r["id"]):
        tests = list_failed(run["id"], prefix=f"[{run['name']}] ")
        total += len(tests)
    print(f"count {total}")


def do_test(test_id):
    # single test: no picklist, just point at it directly (get_result.py handles detail)
    t = get(f"get_test/{test_id}")
    print(f"{test_id}\t{t['title']}\t(run {t['run_id']})")


def parse_url(url):
    m = re.search(r"/(runs|plans|tests)/view/(\d+)", url)
    if not m:
        return None
    kind, id_ = m.group(1), m.group(2)
    return {"runs": "run", "plans": "plan", "tests": "test"}[kind], id_


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    if args[0] in ("--plan", "--run", "--test"):
        kind, id_ = args[0][2:], args[1]
    else:
        arg = args[0]
        parsed = parse_url(arg) if "titan.zebra.lan" in arg or "/view/" in arg else None
        kind, id_ = parsed if parsed else ("run", arg)

    if kind == "plan":
        do_plan(id_)
    elif kind == "test":
        do_test(id_)
    else:
        do_run(id_)


if __name__ == "__main__":
    main()
