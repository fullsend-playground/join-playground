#!/usr/bin/env python3
"""Collect fullsend usage metrics across a GitHub org and post a Slack report.

Scans all repos in the org for slash commands, issues, PRs, and workflow runs.
Stores historical snapshots on a dedicated branch for trend charts.
Charts are generated via QuickChart.io and embedded in Slack Block Kit messages.

Required env vars:
  SLACK_WEBHOOK_URL   Slack incoming-webhook URL (skip with DRY_RUN=true)
  GH_TOKEN            GitHub token with read access to org repos

Optional env vars:
  GITHUB_ORG          Org name (default: fullsend-playground)
  METRICS_REPO        Repo that stores history (default: join-playground)
  REPORT_PERIOD_HOURS Lookback window in hours (default: 1)
  DRY_RUN             true to print payload without posting (default: false)
  QUICKCHART_HOST     QuickChart base URL (default: https://quickchart.io)
"""

import base64
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ── Configuration ───────────────────────────────────────────────────────────

_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def _validate_name(value, label):
    if not _NAME_RE.match(value):
        print(f"Error: {label} contains invalid characters: {value!r}", file=sys.stderr)
        sys.exit(1)
    return value


ORG = _validate_name(os.environ.get("GITHUB_ORG", "fullsend-playground"), "GITHUB_ORG")
METRICS_REPO = _validate_name(
    os.environ.get("METRICS_REPO", "join-playground"), "METRICS_REPO"
)
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")

try:
    PERIOD_HOURS = int(os.environ.get("REPORT_PERIOD_HOURS", "1"))
    if PERIOD_HOURS < 1:
        raise ValueError
except ValueError:
    print("Error: REPORT_PERIOD_HOURS must be a positive integer", file=sys.stderr)
    sys.exit(1)

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

HISTORY_BRANCH = "metrics-data"
HISTORY_PATH = "history.json"
MAX_SNAPSHOTS = 720

QUICKCHART = os.environ.get("QUICKCHART_HOST", "https://quickchart.io")

SLASH_COMMANDS = {
    "/fs-triage", "/fs-code", "/fs-review", "/fs-fix",
    "/fs-retro", "/fs-prioritize",
}


# ── GitHub API ──────────────────────────────────────────────────────────────

def gh(endpoint, *, method="GET", body=None):
    """Call the GitHub REST API via the gh CLI."""
    cmd = ["gh", "api"]
    if method != "GET":
        cmd.extend(["--method", method])
    if body is not None:
        cmd.extend(["--input", "-"])
    cmd.append(endpoint)

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        input=json.dumps(body) if body is not None else None,
    )
    if proc.returncode != 0:
        print(f"  ⚠ {method} {endpoint}: {proc.stderr.strip()}", file=sys.stderr)
        return None
    if not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


# ── Data collection ─────────────────────────────────────────────────────────

def list_repos():
    repos = gh(f"/orgs/{ORG}/repos?type=all&per_page=100")
    return [r["name"] for r in (repos or []) if not r.get("archived")]


def collect_commands(repo, since):
    """Find slash commands in issue/PR comments. Returns (counts, users)."""
    comments = gh(
        f"/repos/{ORG}/{repo}/issues/comments"
        f"?since={since}&per_page=100&sort=updated&direction=desc"
    )
    counts, users = {}, set()
    for c in (comments or []):
        if c.get("created_at", "") < since:
            continue
        body = (c.get("body") or "").strip()
        first_line = body.split("\n", 1)[0].strip()
        tokens = first_line.split()
        if not tokens:
            continue
        cmd = tokens[0].lower()
        if cmd == "/fullsend" and len(tokens) > 1 and tokens[1].lower() == "retro":
            cmd = "/fs-retro"
        if cmd in SLASH_COMMANDS:
            counts[cmd] = counts.get(cmd, 0) + 1
            login = c.get("user", {}).get("login", "")
            if login:
                users.add(login)
    return counts, users


def collect_items(repo, since):
    """Count issues and PRs created in the window. Returns (issues, prs, authors)."""
    items = gh(
        f"/repos/{ORG}/{repo}/issues"
        f"?state=all&since={since}&per_page=100&sort=created&direction=desc"
    )
    issues, prs, authors = 0, 0, set()
    for item in (items or []):
        if item.get("created_at", "") < since:
            continue
        login = item.get("user", {}).get("login", "")
        if login:
            authors.add(login)
        if item.get("pull_request"):
            prs += 1
        else:
            issues += 1
    return issues, prs, authors


def collect_runs(repo, since):
    """Gather workflow runs created in the window, grouped by workflow name."""
    data = gh(f"/repos/{ORG}/{repo}/actions/runs?per_page=100")
    if not data:
        return {}
    run_list = data.get("workflow_runs", []) if isinstance(data, dict) else []

    by_wf = {}
    for run in run_list:
        if run.get("created_at", "") < since:
            continue
        name = run.get("name", "unknown")
        conclusion = run.get("conclusion") or "in_progress"
        event = run.get("event", "unknown")

        if name not in by_wf:
            by_wf[name] = {
                "total": 0, "success": 0, "failure": 0,
                "in_progress": 0, "events": {},
            }
        by_wf[name]["total"] += 1

        if conclusion == "success":
            by_wf[name]["success"] += 1
        elif conclusion in ("failure", "cancelled", "timed_out"):
            by_wf[name]["failure"] += 1
        else:
            by_wf[name]["in_progress"] += 1

        by_wf[name]["events"][event] = by_wf[name]["events"].get(event, 0) + 1

    return by_wf


def collect_all():
    """Orchestrate metric collection across every repo in the org."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=PERIOD_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    repos = list_repos()
    print(f"Scanning {len(repos)} repos since {since}")

    metrics = {
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period_hours": PERIOD_HOURS,
        "repos": {},
        "by_command": {},
        "by_workflow": {},
        "totals": {},
    }

    all_users, all_cmds, all_wfs = set(), {}, {}

    for repo in repos:
        print(f"  → {repo}")

        cmds, cmd_users = collect_commands(repo, since)
        iss, prs, item_authors = collect_items(repo, since)
        wf_runs = collect_runs(repo, since)

        repo_runs = sum(w["total"] for w in wf_runs.values())
        repo_ok = sum(w["success"] for w in wf_runs.values())
        repo_fail = sum(w["failure"] for w in wf_runs.values())
        repo_dispatch = sum(
            w["events"].get("workflow_dispatch", 0)
            + w["events"].get("repository_dispatch", 0)
            for w in wf_runs.values()
        )

        metrics["repos"][repo] = {
            "commands": sum(cmds.values()),
            "issues": iss,
            "prs": prs,
            "runs": repo_runs,
            "successes": repo_ok,
            "failures": repo_fail,
            "dispatches": repo_dispatch,
            "command_detail": cmds,
            "workflow_detail": {k: v["total"] for k, v in wf_runs.items()},
        }

        all_users |= cmd_users | item_authors
        for k, v in cmds.items():
            all_cmds[k] = all_cmds.get(k, 0) + v
        for wf_name, wf_data in wf_runs.items():
            if wf_name not in all_wfs:
                all_wfs[wf_name] = {"total": 0, "success": 0, "failure": 0}
            for field in ("total", "success", "failure"):
                all_wfs[wf_name][field] += wf_data.get(field, 0)

    metrics["by_command"] = all_cmds
    metrics["by_workflow"] = all_wfs
    metrics["totals"] = {
        "commands": sum(all_cmds.values()),
        "issues": sum(r["issues"] for r in metrics["repos"].values()),
        "prs": sum(r["prs"] for r in metrics["repos"].values()),
        "runs": sum(r["runs"] for r in metrics["repos"].values()),
        "successes": sum(r["successes"] for r in metrics["repos"].values()),
        "failures": sum(r["failures"] for r in metrics["repos"].values()),
        "dispatches": sum(r["dispatches"] for r in metrics["repos"].values()),
        "unique_users": len(all_users),
    }
    return metrics


# ── History management ──────────────────────────────────────────────────────

def ensure_branch():
    """Create the metrics-data branch if it doesn't exist yet."""
    ref = gh(f"/repos/{ORG}/{METRICS_REPO}/git/ref/heads/{HISTORY_BRANCH}")
    if ref and "ref" in ref:
        return
    repo_info = gh(f"/repos/{ORG}/{METRICS_REPO}")
    default_branch = (repo_info or {}).get("default_branch", "main")
    default_ref = gh(f"/repos/{ORG}/{METRICS_REPO}/git/ref/heads/{default_branch}")
    if not default_ref:
        print(f"  ⚠ Cannot find {default_branch} branch to seed metrics history", file=sys.stderr)
        return
    gh(
        f"/repos/{ORG}/{METRICS_REPO}/git/refs",
        method="POST",
        body={
            "ref": f"refs/heads/{HISTORY_BRANCH}",
            "sha": default_ref["object"]["sha"],
        },
    )


def load_history():
    """Read snapshot history from the metrics-data branch. Returns (list, sha)."""
    resp = gh(
        f"/repos/{ORG}/{METRICS_REPO}/contents/{HISTORY_PATH}?ref={HISTORY_BRANCH}"
    )
    if not resp or "content" not in resp:
        return [], None
    raw = base64.b64decode(resp["content"]).decode()
    data = json.loads(raw)
    return data.get("snapshots", []), resp.get("sha")


def save_history(snapshots, sha):
    """Write snapshot history back to the metrics-data branch."""
    ensure_branch()
    content = base64.b64encode(
        json.dumps({"snapshots": snapshots}, separators=(",", ":")).encode()
    ).decode()
    body = {
        "message": f"metrics: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
        "content": content,
        "branch": HISTORY_BRANCH,
    }
    if sha:
        body["sha"] = sha
    result = gh(
        f"/repos/{ORG}/{METRICS_REPO}/contents/{HISTORY_PATH}",
        method="PUT",
        body=body,
    )
    if not result or "content" not in result:
        raise RuntimeError("Failed to write history — API returned no content")


def append_snapshot(metrics):
    """Append a compact snapshot to history and persist it. Returns all snapshots."""
    try:
        snapshots, sha = load_history()
    except Exception as exc:
        print(f"  ⚠ Load history failed: {exc}", file=sys.stderr)
        snapshots, sha = [], None

    t = metrics["totals"]
    snapshots.append({
        "ts": metrics["timestamp"],
        "cmds": t["commands"],
        "iss": t["issues"],
        "prs": t["prs"],
        "runs": t["runs"],
        "ok": t["successes"],
        "fail": t["failures"],
        "disp": t["dispatches"],
        "users": t["unique_users"],
        "by_cmd": metrics["by_command"],
        "by_repo": {
            k: {
                "c": v["commands"],
                "i": v["issues"],
                "p": v["prs"],
                "r": v["runs"],
            }
            for k, v in metrics["repos"].items()
        },
    })
    snapshots = snapshots[-MAX_SNAPSHOTS:]

    try:
        save_history(snapshots, sha)
    except Exception as exc:
        print(f"  ⚠ Save history failed: {exc}", file=sys.stderr)

    return snapshots


# ── Chart generation ────────────────────────────────────────────────────────

def quickchart(config, width=600, height=300):
    """POST a Chart.js config to QuickChart.io and return the short image URL."""
    payload = json.dumps({
        "chart": config,
        "width": width,
        "height": height,
        "backgroundColor": "#ffffff",
        "format": "png",
    }).encode()
    req = Request(
        f"{QUICKCHART}/chart/create",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urlopen(req, timeout=15)
        return json.loads(resp.read()).get("url", "")
    except Exception as exc:
        print(f"  ⚠ Chart generation failed: {exc}", file=sys.stderr)
        return ""


def chart_timeline(snapshots):
    """Line chart: commands, workflow runs, and issues+PRs over recent periods."""
    if len(snapshots) < 2:
        return ""
    pts = snapshots[-min(72, len(snapshots)):]
    labels = [s["ts"][5:16].replace("T", " ") for s in pts]
    return quickchart({
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "label": "Agent Commands",
                    "data": [s["cmds"] for s in pts],
                    "borderColor": "#2196F3",
                    "backgroundColor": "rgba(33,150,243,0.1)",
                    "fill": True,
                    "tension": 0.3,
                },
                {
                    "label": "Workflow Runs",
                    "data": [s["runs"] for s in pts],
                    "borderColor": "#4CAF50",
                    "fill": False,
                    "tension": 0.3,
                },
                {
                    "label": "Issues + PRs",
                    "data": [s["iss"] + s["prs"] for s in pts],
                    "borderColor": "#FF9800",
                    "fill": False,
                    "tension": 0.3,
                },
            ],
        },
        "options": {
            "title": {
                "display": True,
                "text": "Activity Over Time",
                "fontSize": 14,
            },
            "scales": {
                "xAxes": [{"ticks": {"maxTicksToShow": 10, "fontSize": 10}}],
                "yAxes": [{"ticks": {"beginAtZero": True}}],
            },
        },
    })


def chart_commands(metrics):
    """Doughnut chart: agent command distribution for the current period."""
    by_cmd = metrics.get("by_command", {})
    if not by_cmd:
        return ""
    labels = list(by_cmd.keys())
    palette = [
        "#2196F3", "#4CAF50", "#FF9800", "#F44336",
        "#9C27B0", "#00BCD4", "#CDDC39", "#795548",
    ]
    return quickchart({
        "type": "doughnut",
        "data": {
            "labels": labels,
            "datasets": [{
                "data": list(by_cmd.values()),
                "backgroundColor": palette[: len(labels)],
            }],
        },
        "options": {
            "title": {
                "display": True,
                "text": "Agent Command Distribution",
                "fontSize": 14,
            },
            "plugins": {
                "datalabels": {
                    "display": True,
                    "color": "#fff",
                    "font": {"weight": "bold"},
                },
            },
        },
    })


def chart_repos(metrics):
    """Bar chart: activity breakdown per repository."""
    repos = metrics.get("repos", {})
    if not repos:
        return ""
    names = sorted(repos.keys())
    return quickchart({
        "type": "bar",
        "data": {
            "labels": names,
            "datasets": [
                {
                    "label": "Commands",
                    "data": [repos[n]["commands"] for n in names],
                    "backgroundColor": "#2196F3",
                },
                {
                    "label": "Issues",
                    "data": [repos[n]["issues"] for n in names],
                    "backgroundColor": "#FF9800",
                },
                {
                    "label": "PRs",
                    "data": [repos[n]["prs"] for n in names],
                    "backgroundColor": "#4CAF50",
                },
                {
                    "label": "Runs",
                    "data": [repos[n]["runs"] for n in names],
                    "backgroundColor": "#9C27B0",
                },
            ],
        },
        "options": {
            "title": {
                "display": True,
                "text": "Activity by Repository",
                "fontSize": 14,
            },
            "scales": {
                "yAxes": [{"ticks": {"beginAtZero": True}}],
            },
        },
    })


# ── Slack message ───────────────────────────────────────────────────────────

def _slack_escape(text):
    """Escape characters that have special meaning in Slack mrkdwn."""
    for ch in ("&", "<", ">", "`", "*", "_", "~"):
        text = text.replace(ch, f"\\{ch}")
    return text


def _arrow(cur, prev):
    if prev is None:
        return ""
    d = cur - prev
    if d > 0:
        return f" ↑{d}"
    if d < 0:
        return f" ↓{abs(d)}"
    return " →"


def _period_label(hours):
    if hours >= 720:
        return f"{hours // 720}mo"
    if hours >= 168:
        return f"{hours // 168}w"
    if hours >= 24:
        return f"{hours // 24}d"
    return f"{hours}h"


def _success_badge(ok, fail):
    total = ok + fail
    if total == 0:
        return "—"
    pct = ok / total * 100
    icon = "🟢" if pct >= 80 else ("🟡" if pct >= 60 else "🔴")
    return f"{icon} {pct:.0f}%"


def build_slack(metrics, snapshots, charts):
    """Assemble a Slack Block Kit payload."""
    t = metrics["totals"]
    prev = snapshots[-2] if len(snapshots) >= 2 else None
    period = _period_label(PERIOD_HOURS)
    quiet = (
        t["commands"] == 0
        and t["runs"] == 0
        and t["issues"] == 0
        and t["prs"] == 0
    )

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📊 Fullsend Usage Report — Last {period}",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"🕐 {metrics['timestamp'][:16].replace('T', ' ')} UTC"
                        f"  •  <https://github.com/{ORG}|{ORG}>"
                    ),
                }
            ],
        },
        {"type": "divider"},
    ]

    if quiet:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"_All quiet — no agent activity in the last {period}._",
                },
            }
        )
    else:
        prev_d = prev or {}

        # ── Summary ──
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "\n".join(
                        [
                            "*Summary*",
                            f"• *Agent commands:* {t['commands']}"
                            f"{_arrow(t['commands'], prev_d.get('cmds'))}",
                            f"• *Issues created:* {t['issues']}"
                            f"{_arrow(t['issues'], prev_d.get('iss'))}",
                            f"• *PRs created:* {t['prs']}"
                            f"{_arrow(t['prs'], prev_d.get('prs'))}",
                            f"• *Workflow runs:* {t['runs']}"
                            f" ({t['successes']} ✅ {t['failures']} ❌)"
                            + (
                                f"  {_success_badge(t['successes'], t['failures'])}"
                                if t["runs"]
                                else ""
                            ),
                            f"• *Dispatches:* {t['dispatches']}"
                            f"{_arrow(t['dispatches'], prev_d.get('disp'))}",
                            f"• *Active users:* {t['unique_users']}",
                        ]
                    ),
                },
            }
        )

        # ── Per-repo breakdown ──
        if metrics["repos"]:
            lines = []
            for name in sorted(metrics["repos"]):
                r = metrics["repos"][name]
                safe = _slack_escape(name)
                lines.append(
                    f"• <https://github.com/{ORG}/{name}|*{safe}*>: "
                    f"{r['commands']} cmd · {r['issues']} iss · "
                    f"{r['prs']} PR · {r['runs']} runs"
                )
            blocks.append({"type": "divider"})
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Repositories*\n" + "\n".join(lines),
                    },
                }
            )

        # ── Agent commands ──
        if metrics["by_command"]:
            parts = [
                f"`{k}` ×{v}"
                for k, v in sorted(
                    metrics["by_command"].items(), key=lambda x: -x[1]
                )
            ]
            blocks.append({"type": "divider"})
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Agent Commands*\n" + "  •  ".join(parts),
                    },
                }
            )

        # ── Workflows ──
        if metrics["by_workflow"]:
            wf_lines = []
            for wf_name in sorted(
                metrics["by_workflow"],
                key=lambda n: -metrics["by_workflow"][n]["total"],
            ):
                w = metrics["by_workflow"][wf_name]
                badge = _success_badge(w["success"], w["failure"])
                safe_wf = _slack_escape(wf_name)
                wf_lines.append(f"• `{safe_wf}`: {w['total']} runs  {badge}")
            blocks.append({"type": "divider"})
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Workflows*\n" + "\n".join(wf_lines),
                    },
                }
            )

    # ── Charts ──
    for url, alt in charts:
        if url:
            blocks.append({"type": "divider"})
            blocks.append({"type": "image", "image_url": url, "alt_text": alt})

    # ── Footer ──
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"📁 <https://github.com/{ORG}|Org>"
                        f" • {len(snapshots)} snapshots stored"
                        f" • <https://github.com/{ORG}/{METRICS_REPO}/actions"
                        f"|Actions>"
                    ),
                }
            ],
        }
    )

    return {"blocks": blocks}


def post_slack(payload):
    data = json.dumps(payload).encode()
    req = Request(
        SLACK_WEBHOOK, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        resp = urlopen(req, timeout=15)
        print(f"✓ Slack: {resp.status}")
    except HTTPError as exc:
        print(f"✗ Slack HTTP {exc.code}", file=sys.stderr)
        sys.exit(1)
    except (URLError, OSError) as exc:
        print(f"✗ Slack connection error: {exc.reason}", file=sys.stderr)
        sys.exit(1)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    if not DRY_RUN and not SLACK_WEBHOOK:
        print(
            "Error: SLACK_WEBHOOK_URL is required (or set DRY_RUN=true)",
            file=sys.stderr,
        )
        sys.exit(1)

    print("=== Fullsend Usage Report ===")
    print(f"Org: {ORG}  Period: {PERIOD_HOURS}h  Dry run: {DRY_RUN}")

    metrics = collect_all()
    t = metrics["totals"]
    print(
        f"\nTotals: {t['commands']} cmds, {t['issues']} iss, {t['prs']} PRs, "
        f"{t['runs']} runs, {t['unique_users']} users"
    )

    snapshots = append_snapshot(metrics)
    print(f"History: {len(snapshots)} snapshots")

    chart_list = [
        (chart_timeline(snapshots), "Activity over time"),
        (chart_commands(metrics), "Agent command distribution"),
        (chart_repos(metrics), "Activity by repository"),
    ]
    ok_count = sum(1 for u, _ in chart_list if u)
    print(f"Charts: {ok_count}/3 generated")

    payload = build_slack(metrics, snapshots, chart_list)

    if DRY_RUN:
        print("\n--- Slack payload (dry run) ---")
        print(json.dumps(payload, indent=2))
    else:
        post_slack(payload)

    print("✓ Done")


if __name__ == "__main__":
    main()
