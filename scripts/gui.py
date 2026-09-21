#!/usr/bin/env python3
"""Standalone tkinter GUI for the test-analysis skill.

Fetches failed tests from a TestRail run/plan/test URL, lets you pick one, then
shells out to the already-authenticated `claude` CLI (non-interactive `-p` mode)
to read the matching adc-titan test source and produce the same root-cause
analysis the test-analysis skill produces inside Claude Code. No API key needed.

Usage:
    uv run python gui.py
"""
import datetime
import glob
import json
import os
import re
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from urllib.parse import urlsplit

import requests

import list_failures
import get_result
from list_failures import parse_url, list_failed, get as tr_get
from get_result import get_failure_comment, get_failure_result, parse_result_stats

DEFAULT_ADC_TITAN_ROOT = (
    r"C:\Users\ad6496\OneDrive - Zebra Technologies\Desktop\GITHUB-Titans\adc-titan"
)
MODELS = ["claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5-20251001"]
PROJECT_ID = 5  # ADC Test Automation

ANALYSIS_PROMPT_TEMPLATE = """\
You are analyzing a failed TestRail test case for a Zebra scanner test automation suite.

{evidence_label}:
---
{comment}
---

Matched test source file(s) from the adc-titan repo:
---
{source}
---

Give a short, concrete answer in three parts, referencing file:line:
1. What the test/sub-case does (from the code, not the log).
2. What the log shows failed (the literal received-vs-expected or error string).
3. Why, grounded in the code path -- e.g. a retry loop that gives up after N attempts,
   a special-cased single-attempt scan with no verification, a blocking call that
   returns before an async condition needed for the test's own trigger, a timing race.
   Don't just restate the log; explain the mechanism in the source that produces that
   log line. If multiple sub-cases failed with the same signature, treat that as one
   root cause, not N independent bugs.

If the test/verification logic looks correct and the log shows the device under test genuinely
returned/produced the bad value (not a framework bug, not obviously transient), say so plainly --
this means the test correctly caught a real firmware/hardware defect, not a flake. Don't default
to "flake" just because you can't find a bug in the test code; a correct test with a real bad
result is a legitimate, common outcome.

Only call it a likely one-off hardware/timing flake if there's a specific reason to doubt
reproducibility (e.g. a known-flaky retry/timing pattern in the code, or a single isolated
failure with no shared signature) -- and say what would need to be checked (rerun, physical log
inspection) rather than inventing a code-level cause.

End with a line starting `**Recommendation:**` on its own, stating one concrete, imperative next
step, picked from:
- Firmware/product bug (source and log both check out, DUT produced a genuinely bad value) ->
  file/reopen a JIRA against the responsible firmware component, citing file:line as repro evidence.
- Test/framework bug (special-case branch is wrong, weak/missing verification) -> note the exact
  file:line to fix and what the fix should do.
- Likely flake, no code-level cause found -> recommend a rerun; escalate to hardware/log inspection
  if it fails again.
"""

JUDGE_PROMPT_TEMPLATE = """\
You are grading a root-cause-analysis writeup produced by an automated tool for a failed
Zebra scanner test. Score it against this rubric -- be strict, don't give credit for
confident-sounding prose that doesn't actually meet the bar:

1. Grounded in the source, not just the log: does it cite specific file:line evidence from
   the matched test source rather than only paraphrasing the log?
2. Actionable recommendation: does it end with one concrete, specific next step (rerun,
   file/reopen a JIRA against a named component, fix a named file:line) -- not a vague
   "investigate further" or a list of options?
3. Not generic/hedgy: does it commit to an actual explanation rather than hedging with
   "could be A or B" when the evidence points one way? Defaulting to "flake" without a
   specific reason to doubt reproducibility should be marked down.
4. Correct fact usage: does it accurately reflect what the log/source actually said (no
   fabricated line numbers, no misquoted values)?

Analysis under review:
---
{analysis}
---

Original evidence (log/comment, first 2000 chars) it was supposed to be grounded in:
---
{evidence_excerpt}
---

Respond with ONLY a JSON object, no other text:
{{"verdict": "PASS" or "FAIL", "score": <0-4 int, how many rubric criteria it met>,
 "reasoning": "<2-4 sentences citing which criteria passed/failed and why>"}}
"""


def judge_analysis(analysis_text, evidence_text, judge_model):
    """Grade an analysis against the quality rubric. Returns a dict or None on failure."""
    prompt = JUDGE_PROMPT_TEMPLATE.format(
        analysis=analysis_text, evidence_excerpt=evidence_text[:2000]
    )
    result = subprocess.run(
        ["claude", "-p", "--model", judge_model, "--output-format", "json", "--tools", ""],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        shell=True,
    )
    if not result.stdout.strip():
        return None
    data = json.loads(result.stdout)
    raw = (data.get("result") or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def find_source(script_name, repo_root):
    patterns = [
        os.path.join(repo_root, "**", f"{script_name}.py"),
        os.path.join(repo_root, "**", f"{script_name}_[a-z].py"),
    ]
    matches = []
    for pattern in patterns:
        matches = glob.glob(pattern, recursive=True)
        if matches:
            break
    return matches


def script_name_from_log_url(url):
    """rsm_attributes_action_091526_163049.txt -> rsm_attributes_action"""
    filename = os.path.basename(urlsplit(url).path)
    stem = os.path.splitext(filename)[0]
    return re.sub(r"(_\d+){2}$", "", stem)


def fetch_log_text(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def run_analysis(evidence_label, evidence_text, script_name, repo_root, model):
    """Build the analysis prompt and call `claude -p`. Returns (text, tokens, cost_usd)."""
    matches = find_source(script_name, repo_root)
    if matches:
        source = "\n\n".join(
            f"--- {path} ---\n{open(path, encoding='utf-8').read()}"
            for path in matches
        )
    else:
        source = f"(no source file found for {script_name})"

    prompt = ANALYSIS_PROMPT_TEMPLATE.format(
        evidence_label=evidence_label, comment=evidence_text, source=source
    )
    result = subprocess.run(
        ["claude", "-p", "--model", model, "--output-format", "json", "--tools", ""],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        shell=True,
    )
    if result.stdout.strip():
        data = json.loads(result.stdout)
        usage = data.get("usage", {})
        total_tokens = (
            usage.get("input_tokens", 0)
            + usage.get("output_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
        )
        cost = data.get("total_cost_usd", 0)
        return (data.get("result") or "(no output)"), total_tokens, cost
    return result.stderr.strip() or "(no output)", 0, 0.0


class App:
    def __init__(self, root):
        self.root = root
        root.title("Test Analysis")
        root.geometry("900x700")

        cred_frame = ttk.Frame(root)
        cred_frame.pack(fill="x", padx=8, pady=4)
        ttk.Label(cred_frame, text="TestRail user:").pack(side="left")
        self.tr_user_var = tk.StringVar(value=os.environ.get("TESTRAIL_USER", ""))
        ttk.Entry(cred_frame, textvariable=self.tr_user_var, width=20).pack(
            side="left", padx=4
        )
        ttk.Label(cred_frame, text="password:").pack(side="left")
        self.tr_password_var = tk.StringVar(value=os.environ.get("TESTRAIL_PASSWORD", ""))
        ttk.Entry(cred_frame, textvariable=self.tr_password_var, show="*", width=20).pack(
            side="left", padx=4
        )
        ttk.Button(cred_frame, text="Save", command=self.save_credentials).pack(side="left")

        model_frame = ttk.Frame(root)
        model_frame.pack(fill="x", padx=8, pady=4)
        ttk.Label(model_frame, text="Model:").pack(side="left")
        self.model_var = tk.StringVar(value=MODELS[0])
        ttk.Combobox(
            model_frame, textvariable=self.model_var, values=MODELS, state="readonly"
        ).pack(side="left", padx=4)

        self.judge_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            model_frame, text="Judge output quality", variable=self.judge_var
        ).pack(side="left", padx=(16, 4))
        self.judge_model_var = tk.StringVar(value="claude-sonnet-5")
        ttk.Combobox(
            model_frame, textvariable=self.judge_model_var, values=MODELS, state="readonly", width=22
        ).pack(side="left", padx=4)

        repo_frame = ttk.Frame(root)
        repo_frame.pack(fill="x", padx=8, pady=4)
        ttk.Label(repo_frame, text="adc-titan repo path:").pack(side="left")
        self.repo_var = tk.StringVar(value=DEFAULT_ADC_TITAN_ROOT)
        ttk.Entry(repo_frame, textvariable=self.repo_var, width=60).pack(
            side="left", padx=4, fill="x", expand=True
        )
        ttk.Button(repo_frame, text="Browse...", command=self.browse_repo).pack(side="left")

        url_frame = ttk.Frame(root)
        url_frame.pack(fill="x", padx=8, pady=4)
        ttk.Label(url_frame, text="TestRail URL:").pack(side="left")
        self.url_var = tk.StringVar()
        ttk.Entry(url_frame, textvariable=self.url_var, width=60).pack(
            side="left", padx=4, fill="x", expand=True
        )
        self.fetch_btn = ttk.Button(
            url_frame, text="Fetch Failures", command=self.fetch_failures
        )
        self.fetch_btn.pack(side="left")
        self.view_retests_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            url_frame, text="View Retests", variable=self.view_retests_var
        ).pack(side="left", padx=(8, 0))

        log_frame = ttk.Frame(root)
        log_frame.pack(fill="x", padx=8, pady=4)
        ttk.Label(log_frame, text="TUSC log URL:").pack(side="left")
        self.log_url_var = tk.StringVar()
        ttk.Entry(log_frame, textvariable=self.log_url_var, width=60).pack(
            side="left", padx=4, fill="x", expand=True
        )
        self.analyze_log_btn = ttk.Button(
            log_frame, text="Analyze From Log URL", command=self.analyze_log_url
        )
        self.analyze_log_btn.pack(side="left")

        browse_frame = ttk.LabelFrame(root, text="Or browse by milestone")
        browse_frame.pack(fill="x", padx=8, pady=4)
        self.load_milestones_btn = ttk.Button(
            browse_frame, text="Load Milestones", command=self.load_milestones
        )
        self.load_milestones_btn.pack(anchor="w", padx=4, pady=2)

        browse_lists = ttk.Frame(browse_frame)
        browse_lists.pack(fill="x", padx=4, pady=2)
        self.milestone_listbox = tk.Listbox(browse_lists, height=6, exportselection=False)
        self.milestone_listbox.pack(side="left", fill="both", expand=True, padx=(0, 4))
        self.milestone_listbox.bind("<<ListboxSelect>>", self.on_milestone_selected)
        self._milestones = []

        self.run_plan_listbox = tk.Listbox(browse_lists, height=6, exportselection=False)
        self.run_plan_listbox.pack(side="left", fill="both", expand=True)
        self.run_plan_listbox.bind("<Double-Button-1>", self.on_run_plan_double_click)
        self._browse_items = []

        ttk.Label(
            browse_frame, text="(double-click a run/plan to fetch its failures)"
        ).pack(anchor="w", padx=4, pady=(0, 2))

        progress_frame = ttk.Frame(root)
        progress_frame.pack(fill="x", padx=8, pady=(0, 4))
        self.status_var = tk.StringVar(value="")
        ttk.Label(progress_frame, textvariable=self.status_var, width=30).pack(side="left")
        self.progress = ttk.Progressbar(progress_frame, orient="horizontal", mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=4)

        filter_frame = ttk.Frame(root)
        filter_frame.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(filter_frame, text="Filter by run:").pack(side="left")
        self.run_filter_var = tk.StringVar(value="All")
        self.run_filter_combo = ttk.Combobox(
            filter_frame, textvariable=self.run_filter_var, values=["All"],
            state="readonly", width=60,
        )
        self.run_filter_combo.pack(side="left", padx=4)
        self.run_filter_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_run_filter())

        tree_frame = ttk.Frame(root)
        tree_frame.pack(fill="x", padx=8, pady=4)
        columns = ("id", "title", "status", "passed", "failed", "total", "date")
        headings = {
            "id": "ID", "title": "Title", "status": "Status",
            "passed": "Passed", "failed": "Failed", "total": "Total",
            "date": "Result Date",
        }
        widths = {
            "id": 90, "title": 380, "status": 70,
            "passed": 60, "failed": 60, "total": 60, "date": 130,
        }
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=10)
        for col in columns:
            self.tree.heading(
                col, text=headings[col],
                command=lambda c=col: self._sort_by_column(c),
            )
            self.tree.column(col, width=widths[col], anchor="w")
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="left", fill="y")
        self._tests = []
        self._sort_reverse = {}

        self.analyze_btn = ttk.Button(
            root, text="Analyze Selected", command=self.analyze_selected
        )
        self.analyze_btn.pack(padx=8, pady=4, anchor="w")

        self.output = tk.Text(root, wrap="word", state="disabled")
        self.output.pack(fill="both", expand=True, padx=8, pady=4)

    def save_credentials(self):
        user = self.tr_user_var.get().strip()
        password = self.tr_password_var.get().strip()
        if not user or not password:
            return
        os.environ["TESTRAIL_USER"] = user
        os.environ["TESTRAIL_PASSWORD"] = password
        subprocess.run(["setx", "TESTRAIL_USER", user], capture_output=True)
        subprocess.run(["setx", "TESTRAIL_PASSWORD", password], capture_output=True)
        messagebox.showinfo("Saved", "TestRail credentials saved for future sessions.")

    def _apply_credentials(self):
        auth = (self.tr_user_var.get().strip(), self.tr_password_var.get().strip())
        list_failures.AUTH = auth
        get_result.AUTH = auth

    def browse_repo(self):
        path = filedialog.askdirectory(
            title="Select adc-titan repo root", initialdir=self.repo_var.get()
        )
        if path:
            self.repo_var.set(path)

    def set_busy(self, busy):
        state = "disabled" if busy else "normal"
        self.fetch_btn.config(state=state)
        self.analyze_btn.config(state=state)
        self.load_milestones_btn.config(state=state)
        self.analyze_log_btn.config(state=state)

    def load_milestones(self):
        self.set_busy(True)
        threading.Thread(target=self._load_milestones_worker, daemon=True).start()

    def _load_milestones_worker(self):
        try:
            self._apply_credentials()
            data = tr_get(f"get_milestones/{PROJECT_ID}")
            milestones = data.get("milestones", data if isinstance(data, list) else [])
            milestones.sort(key=lambda m: m["name"])
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Load failed", str(e)))
            self.root.after(0, lambda: self.set_busy(False))
            return
        self.root.after(0, lambda: self._populate_milestones(milestones))

    def _populate_milestones(self, milestones):
        self._milestones = milestones
        self.milestone_listbox.delete(0, "end")
        for m in milestones:
            self.milestone_listbox.insert("end", m["name"])
        self.set_busy(False)

    def on_milestone_selected(self, event):
        sel = self.milestone_listbox.curselection()
        if not sel:
            return
        milestone_id = self._milestones[sel[0]]["id"]
        self.run_plan_listbox.delete(0, "end")
        self._browse_items = []
        threading.Thread(
            target=self._load_runs_plans_worker, args=(milestone_id,), daemon=True
        ).start()

    def _load_runs_plans_worker(self, milestone_id):
        try:
            self._apply_credentials()
            detail = tr_get(f"get_milestone/{milestone_id}")
            milestone_ids = [milestone_id] + [m["id"] for m in detail.get("milestones", [])]
            items = []
            for mid in milestone_ids:
                runs_data = tr_get(f"get_runs/{PROJECT_ID}&milestone_id={mid}")
                runs = runs_data.get("runs", runs_data if isinstance(runs_data, list) else [])
                plans_data = tr_get(f"get_plans/{PROJECT_ID}&milestone_id={mid}")
                plans = plans_data.get(
                    "plans", plans_data if isinstance(plans_data, list) else []
                )
                items += [{"kind": "run", "id": r["id"], "name": r["name"]} for r in runs]
                items += [{"kind": "plan", "id": p["id"], "name": p["name"]} for p in plans]
            items.sort(key=lambda x: x["name"])
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Load failed", str(e)))
            return
        self.root.after(0, lambda: self._populate_runs_plans(items))

    def _populate_runs_plans(self, items):
        self._browse_items = items
        self.run_plan_listbox.delete(0, "end")
        for item in items:
            self.run_plan_listbox.insert("end", f"[{item['kind']}] {item['name']}")

    def on_run_plan_double_click(self, event):
        sel = self.run_plan_listbox.curselection()
        if not sel:
            return
        item = self._browse_items[sel[0]]
        self.start_fetch(item["kind"], item["id"])

    def write_output(self, text):
        self.output.config(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)
        self.output.config(state="disabled")

    def fetch_failures(self):
        url = self.url_var.get().strip()
        if not url:
            return
        parsed = parse_url(url) if ("titan.zebra.lan" in url or "/view/" in url) else None
        kind, id_ = parsed if parsed else ("run", url)
        self.start_fetch(kind, id_)

    def start_fetch(self, kind, id_):
        self.set_busy(True)
        threading.Thread(
            target=self._fetch_failures_worker, args=(kind, id_), daemon=True
        ).start()

    def _set_progress(self, done, total, message):
        if total <= 0:
            self.progress.config(mode="determinate", maximum=1, value=0)
            self.status_var.set(message)
            return
        self.progress.config(mode="determinate", maximum=total, value=done)
        self.status_var.set(f"{message} {done}/{total}")

    def _fetch_failures_worker(self, kind, id_):
        status_ids = "4,5" if self.view_retests_var.get() else "5"
        self.root.after(0, lambda: self._set_progress(0, 0, "Fetching test list..."))
        try:
            self._apply_credentials()
            if kind == "plan":
                plan = tr_get(f"get_plan/{id_}")
                runs = [r for entry in plan.get("entries", []) for r in entry.get("runs", [])]
                tests = []
                for run in sorted(runs, key=lambda r: r["id"]):
                    run_tests = list_failed(
                        run["id"], prefix=f"[{run['name']}] ", status_ids=status_ids
                    )
                    for t in run_tests:
                        t["run_name"] = run["name"]
                    tests.extend(run_tests)
            elif kind == "test":
                t = tr_get(f"get_test/{id_}")
                t["run_name"] = ""
                tests = [t]
            else:
                tests = list_failed(id_, status_ids=status_ids)
                for t in tests:
                    t["run_name"] = ""
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Fetch failed", str(e)))
            self.root.after(0, lambda: self.set_busy(False))
            self.root.after(0, lambda: self._set_progress(0, 0, ""))
            return

        total = len(tests)
        for i, t in enumerate(tests):
            self.root.after(0, lambda i=i: self._set_progress(i, total, "Fetching stats..."))
            try:
                result = get_failure_result(t["id"])
            except Exception:
                result = None
            t["comment"] = result["comment"] if result else None
            t["comment_created_on"] = result["created_on"] if result else None
            t["stats"] = parse_result_stats(t["comment"])

        self.root.after(0, lambda: self._set_progress(total, total, "Done."))
        self.root.after(0, lambda: self._populate_listbox(tests))

    def _populate_listbox(self, tests):
        self._all_tests = tests
        run_names = sorted({t["run_name"] for t in tests if t.get("run_name")})
        self.run_filter_combo["values"] = ["All"] + run_names
        self.run_filter_var.set("All")
        self._apply_run_filter()
        self.set_busy(False)

    def _apply_run_filter(self):
        selected = self.run_filter_var.get()
        if selected == "All":
            tests = self._all_tests
        else:
            tests = [t for t in self._all_tests if t.get("run_name") == selected]
        self._populate_filtered_listbox(tests)

    def _populate_filtered_listbox(self, tests):
        self._tests = tests
        for item in self.tree.get_children():
            self.tree.delete(item)
        for idx, t in enumerate(tests):
            status = "RETEST" if t.get("status_id") == 4 else ""
            stats = t.get("stats") or {}
            created_on = t.get("comment_created_on")
            date_str = (
                datetime.datetime.fromtimestamp(created_on).strftime("%Y-%m-%d %H:%M")
                if created_on else ""
            )
            self.tree.insert(
                "", "end", iid=str(idx),
                values=(
                    t["id"], t["title"], status,
                    stats.get("Passed", ""), stats.get("Failed", ""),
                    stats.get("Tests", ""), date_str,
                ),
            )

    def _sort_by_column(self, col):
        keys = {
            "id": lambda t: t["id"],
            "title": lambda t: t["title"].lower(),
            "status": lambda t: t.get("status_id", 0),
            "passed": lambda t: (t.get("stats") or {}).get("Passed", -1),
            "failed": lambda t: (t.get("stats") or {}).get("Failed", -1),
            "total": lambda t: (t.get("stats") or {}).get("Tests", -1),
            "date": lambda t: t.get("comment_created_on") or 0,
        }
        reverse = self._sort_reverse.get(col, False)
        self._tests.sort(key=keys[col], reverse=reverse)
        self._sort_reverse[col] = not reverse
        self._populate_filtered_listbox(self._tests)

    def analyze_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        test = self._tests[int(sel[0])]
        self.set_busy(True)
        self.write_output("Analyzing...")
        threading.Thread(target=self._analyze_worker, args=(test,), daemon=True).start()

    def _analyze_worker(self, test):
        try:
            self._apply_credentials()
            comment = test.get("comment") or get_failure_comment(test["id"]) or "(no comment found)"
            label = (
                "Failure comment from most recent Failed result (test is currently"
                " marked Retest)"
                if test.get("status_id") == 4
                else "Failure comment from TestRail (log summary + failed sub-cases)"
            )
            text = self._run_analysis(label, comment, test["title"])
        except Exception as e:
            text = f"Error: {e}"
        self.root.after(0, lambda: self.write_output(text))
        self.root.after(0, lambda: self.set_busy(False))

    def analyze_log_url(self):
        url = self.log_url_var.get().strip()
        if not url:
            return
        self.set_busy(True)
        self.write_output("Fetching log and analyzing...")
        threading.Thread(target=self._analyze_log_worker, args=(url,), daemon=True).start()

    def _analyze_log_worker(self, url):
        try:
            log_text = fetch_log_text(url)
            script_name = script_name_from_log_url(url)
            text = self._run_analysis(
                f"TUSC log file ({url})", log_text, script_name
            )
        except Exception as e:
            text = f"Error: {e}"
        self.root.after(0, lambda: self.write_output(text))
        self.root.after(0, lambda: self.set_busy(False))

    def _run_analysis(self, evidence_label, evidence_text, script_name):
        text, total_tokens, cost = run_analysis(
            evidence_label,
            evidence_text,
            script_name,
            self.repo_var.get().strip(),
            self.model_var.get(),
        )
        footer = f"\n\n---\n{total_tokens} tokens, ${cost:.4f}"

        if self.judge_var.get():
            verdict = judge_analysis(text, evidence_text, self.judge_model_var.get())
            if verdict:
                footer += (
                    f"\n\n--- Judge ({self.judge_model_var.get()}) ---\n"
                    f"Verdict: {verdict.get('verdict')}  Score: {verdict.get('score')}/4\n"
                    f"Reasoning: {verdict.get('reasoning')}"
                )
            else:
                footer += "\n\n--- Judge ---\n(judge call failed or returned invalid output)"

        return text + footer


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
