#!/usr/bin/env python3
"""Print the most recent result comment for a TestRail test.

Usage:
    uv run python get_result.py <test_id>
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


def get_last_comment(test_id):
    results = get(f"get_results/{test_id}")["results"]
    for result in results:
        if result.get("comment"):
            return result["comment"]
    return None


def get_failure_result(test_id):
    """Return the most recent Failed-status result dict, even if the test's current status
    is Retest (a bare retest marker usually has no useful comment) -- falls back to the
    newest result of any status that has a comment. Returns None if nothing has a comment."""
    results = get(f"get_results/{test_id}")["results"]
    for result in results:
        if result.get("status_id") == 5 and result.get("comment"):
            return result
    for result in results:
        if result.get("comment"):
            return result
    return None


def get_failure_comment(test_id):
    result = get_failure_result(test_id)
    return result["comment"] if result else None


def parse_result_stats(comment):
    """Pull Tests/Passed/Failed/Skipped counts out of the log-summary block in a comment."""
    if not comment:
        return {}
    stats = {}
    for key in ("Tests", "Passed", "Failed", "Skipped"):
        m = re.search(rf"{key}:\s*(\d+)", comment)
        if m:
            stats[key] = int(m.group(1))
    return stats


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    print(get_last_comment(sys.argv[1]))


if __name__ == "__main__":
    main()
