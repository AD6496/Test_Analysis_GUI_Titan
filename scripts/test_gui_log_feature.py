#!/usr/bin/env python3
"""Test the GUI's log-URL analysis feature end-to-end, then judge output quality.

Usage:
    uv run python scripts/test_gui_log_feature.py [--model MODEL] [--judge-model MODEL] [--url URL]

Options:
    --model MODEL         Model that generates the analysis (default: claude-haiku-4-5-20251001)
    --judge-model MODEL   Model that scores the analysis (default: claude-sonnet-5)
    --url URL             Log file URL to test
                          (default: http://tusc44.zebra.lan/logs/rsm_attributes_action_091526_163049.txt)

Examples:
    uv run python scripts/test_gui_log_feature.py
    uv run python scripts/test_gui_log_feature.py --model claude-sonnet-5
    uv run python scripts/test_gui_log_feature.py --url http://tusc44.zebra.lan/logs/some_other_log.txt
"""
import sys
import argparse
from gui import (
    fetch_log_text,
    script_name_from_log_url,
    find_source,
    run_analysis,
    judge_analysis as _judge_analysis,
    DEFAULT_ADC_TITAN_ROOT,
)


def test_script_name_derivation(url):
    print("Test 1: Script name derivation")
    print(f"  URL: {url}")
    result = script_name_from_log_url(url)
    print(f"  Derived script name: {result}")
    return result


def test_log_fetch(url):
    print("\nTest 2: Log fetch")
    print(f"  URL: {url}")
    try:
        text = fetch_log_text(url)
        print(f"  SUCCESS: Fetched {len(text)} bytes")
        return text
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        return None


def test_source_find(script_name, repo_root):
    print(f"\nTest 3: Source file search")
    print(f"  Script name: {script_name}")
    print(f"  Repo root: {repo_root}")
    matches = find_source(script_name, repo_root)
    if matches:
        print(f"  FOUND {len(matches)} match(es):")
        for m in matches:
            print(f"    - {m}")
    else:
        print(f"  NO MATCHES FOUND")
    return matches


def judge_analysis(analysis_text, log_text, judge_model):
    print(f"\nTest 5: Judging analysis quality (model: {judge_model})")
    verdict = _judge_analysis(analysis_text, log_text, judge_model)
    if not verdict:
        print("  FAILED: no judge output or invalid JSON")
        return None
    print(f"  Verdict: {verdict.get('verdict')}  Score: {verdict.get('score')}/4")
    print(f"  Reasoning: {verdict.get('reasoning')}")
    return verdict


def main():
    parser = argparse.ArgumentParser(
        description="Test the GUI's log-URL analysis feature end-to-end, then judge quality"
    )
    parser.add_argument("--model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--judge-model", default="claude-sonnet-5")
    parser.add_argument(
        "--url",
        default="http://tusc44.zebra.lan/logs/rsm_attributes_action_091526_163049.txt",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("GUI LOG-URL FEATURE TEST")
    print("=" * 70)

    script_name = test_script_name_derivation(args.url)
    if not script_name:
        print("\n[CRITICAL] Script name derivation failed")
        return 1

    log_text = test_log_fetch(args.url)
    if not log_text:
        print("\n[CRITICAL] Log fetch failed — network unreachable or URL invalid")
        return 1

    matches = test_source_find(script_name, DEFAULT_ADC_TITAN_ROOT)
    if not matches:
        print("\n[WARNING] No source file found — analysis will indicate this")

    print(f"\nTest 4: Analysis pipeline (model: {args.model})")
    text, total_tokens, cost, _matches = run_analysis(
        "TUSC log file", log_text, script_name, DEFAULT_ADC_TITAN_ROOT, args.model
    )
    print(f"  Tokens: {total_tokens}")
    print(f"  Cost: ${cost:.6f}")
    print("\n" + "=" * 70)
    print("ANALYSIS OUTPUT:")
    print("=" * 70)
    print(text)
    print("=" * 70)

    verdict = judge_analysis(text, log_text, args.judge_model)

    print("\n" + "=" * 70)
    if verdict and verdict.get("verdict") == "PASS":
        print(f"RESULT: PASS ({verdict.get('score')}/4)")
        print("=" * 70)
        return 0
    score_note = f" ({verdict.get('score')}/4)" if verdict else " (judge error)"
    print(f"RESULT: FAIL{score_note}")
    print("=" * 70)
    return 1


if __name__ == "__main__":
    sys.exit(main())
