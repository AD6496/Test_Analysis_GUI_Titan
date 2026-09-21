---
name: test-analysis
description: Given a TestRail run/test/plan URL from titan.zebra.lan, list the failed tests in that run, let the user pick one, then explain WHY it failed by cross-referencing the failure comment/log against the actual adc-titan .py test source. Use when the user pastes a titan.zebra.lan TestRail link and asks about failures, or asks to analyze/investigate a test failure.
---

# TestRail Failure Analysis

Turns a TestRail run/plan/test URL into: failed-test picklist → root-cause analysis grounded
in the actual Python test source (not just the log text).

## Environment

- **Base URL**: `http://titan.zebra.lan/testrail/index.php?/api/v2`
- **Auth**: HTTP Basic Auth via `TESTRAIL_USER`/`TESTRAIL_PASSWORD` env vars (see
  `scripts/get_result.py`/`scripts/list_failures.py`) — the working creds are a zebra.com email
  used as both username and password. If you hit a 401, the working pair may have changed;
  ask the user for the current one rather than guessing, and don't hardcode it back into this
  file — it's meant to stay out of source control.
- **Python**: `uv run python`, `requests` — no special SSL flags (internal host, plain http)
- **Shell**: run these scripts via the PowerShell tool, not Bash — Bash (git-bash/MSYS) on this
  machine prints ~15 lines of `/etc/*` symlink permission-denied noise on every single
  invocation regardless of command, which wastes tokens and adds nothing.
- **adc-titan repo** (source of truth for test logic):
  `C:\Users\ad6496\OneDrive - Zebra Technologies\Desktop\GITHUB-Titans\adc-titan`
  Test scripts live under `python\<module>\<test_name>.py`, e.g.
  `python\ocr\ocr_templates_ocra.py`, `python\system\system_host_id_b.py`,
  `python\ssi\ssi_decoder_nak.py`.

- **Scripts** (in `scripts/` next to this file, use these instead of inlining requests calls):
  - `list_failures.py <url-or-run_id>` — Step 1+2, one call. Pass the raw pasted URL
    (or bare run/plan/test id) straight through; it detects `runs/view`, `plans/view`,
    `tests/view` itself and dispatches. No manual URL parsing needed.
  - `get_result.py <test_id>` — Step 3 failure comment

## Step 1+2 — list failed tests, let the user pick

```
uv run python scripts/list_failures.py <the-url-the-user-pasted>
```

Output is one line per failed test: `<test_id>\t<title>` (plan mode prefixes each with
`[run name]`), then `count N`. Single-test URLs print just that test's id/title/run_id
directly (no picklist — there's nothing to pick).

Uses `status_id=5` (Failed) under the hood. Don't hardcode that blindly forever — if it ever
looks wrong (empty list on a run you know has failures), call `get_statuses` once and resolve
the "Failed" id dynamically.

Print the script's raw stdout verbatim, unmodified — do not renumber, reformat, or summarize
it. Do not state the count separately or add any text of your own, and never use
`AskUserQuestion` here (it caps at 4 options and hides the rest). Then ask the user to reply
with a test_id and wait.

**This must go in your actual chat text output, inside a code block, not just live in the
tool call result.** The host UI collapses PowerShell/Bash tool calls behind a "Ran 1 shell
command" toggle that's collapsed by default — if the picklist only exists in the tool
result, the user sees nothing. Paste the stdout into your own message.

## Step 3 — pull the failure detail for the picked test

```
uv run python scripts/get_result.py <test_id>
```

Prints the most recent result's `comment`.

The comment format (confirmed pattern, from the script's own log-summary block) is:

```
<log URL(s)>

**Tests failed, see log for details.**
Elapsed: <time>

Tests: <N>
Passed: <N>
Failed: <N>

|||:Test ID|:Result|:Feature|:Summary
||<Polarion/TestRail sub-case ID>|Failed|<feature description>|<failure reason string>
```

Parse out:
- The **script name** (test title from Step 2, e.g. `ocr_templates_ocra`) — this is the
  `.py` file to go read, not the sub-case.
  - **Modules can be split across suffixed files** — if `<script>.py` doesn't exist, glob
    for `<script>_[a-z].py` (e.g. `system_host_id` splits into `system_host_id_a.py`
    through `_f.py`). Use the sub-case ID (e.g. `TC-22742`) to find which suffix file
    actually contains it if there's ambiguity.
- Each failed sub-case line: its ID, feature/description text, and failure reason
  (`MISDECODE: ...`, `FAILED TO DECODE: ...`, `RSP RECV: None`, etc). If multiple sub-cases
  failed, look for a **shared pattern** (same feature/rule, same wrong value received,
  same symbology) before treating them as N independent bugs — a repeated identical
  signature across many sub-cases usually means one root cause manifesting once per
  sub-case (see the `system_host_id_b` TC-22742 precedent: 38 failures, all "expected 5632
  got 36864" → one rule-scan that silently didn't apply, not 38 misdecodes).

## Step 4 — find and read the actual source

```
Glob: **/<script_name>.py   under C:\Users\ad6496\OneDrive - Zebra Technologies\Desktop\GITHUB-Titans\adc-titan
```
Read the whole file (these test files are short, ~50-150 lines). Identify:
- The specific test function/Feature entry matching the failed sub-case ID (search for the
  literal ID string, e.g. `"TC-15061"` or `test_id in ['TC-22742', ...]`).
- Any conditional/special-case branches keyed off that same ID, model name, or device
  capability (`t.cordless`, `t.is_plankton`, `t.model in [...]`) — these are exactly where
  weakened verification or skipped feedback checks live, and are disproportionately likely
  to explain "why did this fail" vs. a generic assert.
- What the assert actually checks and what value it compares against, so the analysis ties
  the log's literal failure string back to a specific line of code, not just a paraphrase
  of the log.

## Step 5 — answer

Give a short, concrete answer in three parts, referencing `file:line`:
1. **What the test/sub-case does** (1-2 sentences, from the code, not the log).
2. **What the log shows failed** (the literal received-vs-expected or error string).
3. **Why**, grounded in the code path — e.g. a retry loop that gives up after N attempts,
   a special-cased single-attempt scan with no verification, a blocking call that returns
   before an async condition needed for the test's own trigger, a timing race. Don't just
   restate the log; explain the mechanism in the source that produces that log line.

If nothing in the source obviously explains it (assert looks straightforward, no special
casing), say so plainly instead of speculating — call it a likely one-off hardware/timing
flake and note what would need to be checked (rerun, physical log inspection) rather than
inventing a code-level cause.

## Step 6 — recommend a course of action

End with a line starting `**Recommendation:**` on its own — never fold it into the Step 5
prose or bury it inside a longer sentence. State it as a direct imperative telling the human
what to do next (rerun X, file JIRA against Y citing file:line, pull the USB capture from
tusc<N>), not a description of what "would" happen. One concrete recommendation, picked from
(don't list all options, just state the one that fits):
- **Firmware/product bug** (code path genuinely produces the bad value) → file/reopen a
  JIRA against the responsible firmware component, cite the file:line mechanism as repro
  evidence.
- **Test/framework bug** (special-case branch is wrong, missing device in a model list,
  weak/missing verification) → note the exact file:line to fix and what the fix should do.
- **Likely flake, no code-level cause found** → recommend a rerun; if it fails again,
  escalate to physical log/hardware inspection instead of re-analyzing the same log.
- **Shared-pattern failure across many sub-cases** → recommend treating it as the one
  underlying issue, not N tickets.

If a JIRA/Polarion ID for the underlying bug already exists in the source or log (e.g.
`DC-28409`), say whether this result confirms it's still open/regressed vs. looks unrelated.

## Standalone GUI

A tkinter GUI (`scripts/gui.py`) reimplements this whole flow outside Claude Code —
fetch failures by URL or by browsing milestone → run/plan, pick one, click "Analyze
Selected", and it shells out to the non-interactive `claude -p` CLI (no API key needed)
with a baked-in prompt equivalent to Steps 4-6 above.

- Run from source: `uv run python scripts/gui.py`
- Packaged build: `scripts/TestAnalysisGUI.spec` (PyInstaller) → `scripts/dist/TestAnalysisGUI.exe`
  / `TestAnalysisGUI.zip`. Rebuild with `uv run pyinstaller TestAnalysisGUI.spec` from `scripts/`.
- TestRail creds and adc-titan repo path are editable in the GUI (creds save via `setx` to
  `TESTRAIL_USER`/`TESTRAIL_PASSWORD`); repo path defaults to the same adc-titan path as above.
- This is a separate, independently-maintained path — keep its `ANALYSIS_PROMPT_TEMPLATE` and
  `find_source` logic in sync with Steps 3-6 above by hand if those change.

## GUI Log-URL Feature Testing — quality eval, not just plumbing

The point of this script is answering "did the analysis logic produce good, actionable output
for this log?", not just "did the fetch/glob steps not crash." It exercises the GUI's log-URL
path (via `gui.run_analysis()`, the same function the GUI itself calls — no duplicated logic)
and then makes a **second** `claude -p` call with a strict rubric to grade the first call's
output, so the verdict doesn't depend on a human or agent eyeballing raw text each time.

```
uv run python scripts/test_gui_log_feature.py [--model MODEL] [--judge-model MODEL] [--url URL]
```

**Options:**
- `--model MODEL` — model that generates the analysis (default: `claude-haiku-4-5-20251001`)
- `--judge-model MODEL` — model that grades it (default: `claude-sonnet-5` — deliberately a
  stronger/different model than the default generator, so it isn't grading its own work)
- `--url URL` — log file URL to test (default: `http://tusc44.zebra.lan/logs/rsm_attributes_action_091526_163049.txt`)

**Rubric the judge scores against** (0-4, see `JUDGE_PROMPT_TEMPLATE` in the script to tune):
1. Grounded in file:line source evidence, not just a log paraphrase.
2. Ends with one concrete, specific recommendation — not "investigate further."
3. Commits to an explanation instead of hedging or defaulting to "flake" without a specific
   reason to doubt reproducibility.
4. Facts (line numbers, values) actually match what the log/source said.

**Exit code / output:** prints all 5 phases (script-name derivation, log fetch, source match,
analysis, judge verdict) and exits 0 only on judge `PASS`; exits 1 on any earlier failure or a
`FAIL`/error verdict — so it's scriptable for repeated runs, not just a one-off manual read.

## Notes

- Never write to TestRail (no `add_result`, no comments) — this skill is read-only analysis.
- If `get_test`/`get_results` 401s, the auth pair may have changed again — ask the user.
