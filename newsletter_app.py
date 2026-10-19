#!/usr/bin/env python3
"""Loopback-only local approval dashboard for AI Pulse."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
ISSUES = ROOT / "issues"
COLLECTED = ROOT / "data" / "collected"
HOST = "127.0.0.1"
PORT = int(os.environ.get("NEWSLETTER_PORT", "8765"))
APPROVAL_SLOTS = (
    "lead_ai", "ai_healthcare", "other_ai", "health_insurance_india_a", "health_insurance_india_b"
)
SLOT_LABELS = {
    "lead_ai": "Lead AI story",
    "ai_healthcare": "AI in healthcare",
    "other_ai": "Other AI field",
    "health_insurance_india_a": "Health Insurance in India · story 1",
    "health_insurance_india_b": "Health Insurance in India · story 2",
}
STATE = {"busy": False, "step": "", "error": "", "week": "", "lock": threading.Lock()}


def load_env_file():
    for env_file in (ROOT / ".env", ROOT / "newsletter.env"):
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def monday_date(now=None):
    today = (now or datetime.now().astimezone()).date()
    return today - timedelta(days=today.weekday())


def week_key(day=None):
    return (day or monday_date()).strftime("%G-W%V")


def paths_for_week(day=None):
    day = day or monday_date()
    key = week_key(day)
    ranking_path = ISSUES / f"ranking_{key}.json"
    approval_path = ISSUES / f"approval_{key}.json"
    if not ranking_path.exists():
        # Pick up the first manually assembled review packet, named by collection date.
        today_key = datetime.now().astimezone().date().isoformat()
        dated_ranking = ISSUES / f"ranking_{today_key}.json"
        dated_approval = ISSUES / f"approval_{today_key}.json"
        if dated_ranking.exists() and dated_approval.exists():
            ranking_path, approval_path = dated_ranking, dated_approval
    return key, ranking_path, approval_path


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def next_issue_number():
    current = 0
    for path in (ROOT / "letter").glob("*.html"):
        match = re.search(r"Issue\s*#(\d+)", path.read_text(encoding="utf-8", errors="ignore"), re.I)
        if match:
            current = max(current, int(match.group(1)))
    for path in ISSUES.glob("issue_*.json"):
        doc = read_json(path) or {}
        current = max(current, int(doc.get("issue_number", 0) or 0))
    return current + 1


def run_command(argv, step):
    STATE["step"] = step
    completed = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=900)
    if completed.returncode:
        output = (completed.stdout + "\n" + completed.stderr).strip()
        raise RuntimeError(f"{step} failed. {output[-3500:]}")
    return completed.stdout.strip()


def run_weekly(force=False):
    load_env_file()
    with STATE["lock"]:
        if STATE["busy"]:
            return
        key, ranking_path, approval_path = paths_for_week()
        if not force and ranking_path.exists() and approval_path.exists():
            STATE.update({"week": key, "step": "ready for approval", "error": ""})
            return
        STATE.update({"busy": True, "week": key, "step": "collecting this week's news", "error": ""})
    try:
        cmd = [sys.executable, str(ROOT / "newsletter_workflow.py")]
        run_command(cmd + ["collect"], "News collection")
        run_command(cmd + ["rank", "--input", str(COLLECTED / "ai_news_latest.json"),
                           str(COLLECTED / "health_insurance_india_latest.json"),
                           "--output", str(ranking_path)], "AI ranking")
        run_command(cmd + ["approve", "--ranking", str(ranking_path), "--output", str(approval_path)], "Approval sheet")
        STATE.update({"step": "ready for approval", "error": ""})
    except Exception as exc:  # surfaced to the local dashboard for recovery
        STATE.update({"step": "needs attention", "error": str(exc)})
    finally:
        STATE.update({"busy": False})


def run_in_thread(target, *args):
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()


class Handler(BaseHTTPRequestHandler):
    server_version = "AIPulseLocal/1.0"

    def log_message(self, fmt, *args):
        print("[dashboard] " + fmt % args)

    def send_json(self, status, body):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def send_file(self, path, content_type):
        if not path.is_file():
            self.send_error(404, "Not found")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body_json(self):
        if self.headers.get("Content-Length", "0").isdigit() is False:
            raise ValueError("Invalid request length")
        length = int(self.headers.get("Content-Length", "0"))
        if length > 100_000:
            raise ValueError("Request too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            return self.send_file(ROOT / "newsletter_dashboard.html", "text/html; charset=utf-8")
        if path == "/api/status":
            load_env_file()
            key, ranking_path, approval_path = paths_for_week()
            ranking = read_json(ranking_path)
            approval = read_json(approval_path)
            today = datetime.now().astimezone().date()
            # Issues approved before Monday are dated for the next Monday morning.
            issue_day = monday_date() if today.weekday() == 0 else monday_date() + timedelta(days=7)
            generated = approval.get("generated_html") if approval else None
            if generated and not Path(generated).is_absolute():
                generated = str((ROOT / generated).resolve())
            html_path = Path(generated) if generated else ISSUES / f"issue_{(monday_date().strftime('%d-%m-%Y'))}.html"
            status = {k: v for k, v in STATE.items() if k != "lock"}
            status.update({"week": key, "monday": monday_date().isoformat(), "has_ranking": bool(ranking),
                           "has_approval": bool(approval), "approval_status": approval.get("status") if approval else None,
                           "approved_ids": approval.get("approved_ids", {}) if approval else {},
                           "issue_number": next_issue_number(), "issue_date": issue_day.strftime("%d/%m/%Y"),
                           "issue_date_iso": issue_day.isoformat(),
                           "api_key_configured": bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY")),
                           "ai_provider": "Gemini" if os.environ.get("GEMINI_API_KEY") else ("OpenAI" if os.environ.get("OPENAI_API_KEY") else None),
                           "generated": html_path.exists(), "generated_html": str(html_path) if html_path.exists() else None,
                           "weekday": datetime.now().astimezone().weekday(),
                           "local_hour": datetime.now().astimezone().hour})
            return self.send_json(200, status)
        if path == "/api/ranking":
            _, ranking_path, _ = paths_for_week()
            ranking = read_json(ranking_path)
            return self.send_json(200, ranking or {"error": "No ranking for this week yet"})
        if path == "/api/preview":
            _, _, approval_path = paths_for_week()
            approval = read_json(approval_path) or {}
            file_path = approval.get("generated_html")
            if file_path:
                file_path = Path(file_path)
                if not file_path.is_absolute():
                    file_path = (ROOT / file_path).resolve()
                if file_path.resolve().parent == ISSUES.resolve():
                    return self.send_file(file_path, "text/html; charset=utf-8")
            fallback = ISSUES / f"issue_{monday_date().strftime('%d-%m-%Y')}.html"
            return self.send_file(fallback, "text/html; charset=utf-8")
        if path == "/api/download":
            _, _, approval_path = paths_for_week()
            approval = read_json(approval_path) or {}
            file_path = approval.get("generated_html")
            if not file_path:
                return self.send_error(404, "Generate the approved issue first")
            file_path = Path(file_path)
            if not file_path.is_absolute():
                file_path = (ROOT / file_path).resolve()
            if file_path.resolve().parent != ISSUES.resolve():
                return self.send_error(403, "Invalid issue path")
            if not file_path.is_file():
                return self.send_error(404, "Issue file not found")
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{file_path.name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        return self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self.body_json()
            if path == "/api/run":
                if STATE["busy"]:
                    return self.send_json(409, {"error": "The weekly process is already running."})
                run_in_thread(run_weekly, bool(data.get("force")))
                return self.send_json(202, {"status": "started"})
            if path == "/api/approve-generate":
                load_env_file()
                if STATE["busy"]:
                    return self.send_json(409, {"error": "Another workflow action is already running."})
                _, ranking_path, approval_path = paths_for_week()
                ranking = read_json(ranking_path)
                if not ranking:
                    return self.send_json(409, {"error": "No ranking is ready yet."})
                from newsletter_workflow import SLOTS, validate_approval
                approval = read_json(approval_path) or {}
                approval["approved_ids"] = {slot: str(data.get("approved_ids", {}).get(slot, "")) for slot in SLOTS}
                approval["ranking_file"] = str(ranking_path.resolve())
                approval["status"] = "approved"
                manual_items = data.get("manual_stories", {})
                if not isinstance(manual_items, dict):
                    return self.send_json(400, {"error": "Manual story details are invalid."})
                from datetime import timezone
                from newsletter_workflow import in_window
                now = datetime.now(timezone.utc)
                cutoff = now - timedelta(hours=168)
                for slot, story in manual_items.items():
                    if slot not in SLOTS or not isinstance(story, dict):
                        return self.send_json(400, {"error": "Manual story is assigned to an invalid section."})
                    title = str(story.get("title", "")).strip()
                    url = str(story.get("url", "")).strip()
                    summary = str(story.get("summary", "")).strip()
                    publisher = str(story.get("publisher", "")).strip()
                    published_at = str(story.get("published_at", "")).strip()
                    parsed_url = urlparse(url)
                    if not title or not summary or not publisher or parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
                        return self.send_json(400, {"error": "For each manual story, enter a headline, publisher, http(s) source URL, and summary."})
                    try:
                        published_dt = datetime.fromisoformat(published_at).astimezone(timezone.utc)
                    except (ValueError, TypeError):
                        return self.send_json(400, {"error": "Enter a valid publication date and time for each manual story."})
                    if not cutoff <= published_dt <= now:
                        return self.send_json(400, {"error": "Manual stories must have been published within the past seven days."})
                    item_id = "manual-" + uuid.uuid4().hex[:16]
                    candidate = {"id": item_id, "title": title, "url": url, "source": publisher,
                                 "publisher": publisher, "summary": summary, "published_at": published_dt.isoformat(),
                                 "published_date": published_dt.astimezone().strftime("%Y-%m-%d"), "manual": True}
                    ranking.setdefault("candidates", []).append(candidate)
                    ranking.setdefault("slots", {}).setdefault(slot, []).append({
                        "id": item_id, "reason": "Manually added by the editor.",
                        "source_quality": "Editor supplied; verify the linked source.",
                        "caveat": "Manually added story; confirm the facts against the source.", "confidence": "editor review"})
                    approval["approved_ids"][slot] = item_id
                try:
                    if int(data.get("issue_number", 0)) < 1:
                        raise ValueError("Issue number must be positive.")
                    datetime.strptime(str(data.get("issue_date", "")), "%d/%m/%Y")
                except ValueError:
                    return self.send_json(400, {"error": "Enter a positive issue number and a valid issue date."})
                try:
                    validate_approval(approval, ranking)
                except RuntimeError as exc:
                    return self.send_json(400, {"error": str(exc)})
                if manual_items:
                    ranking_path.write_text(json.dumps(ranking, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                approval["approved_at"] = datetime.now().astimezone().isoformat()
                approval["approved_via"] = "local_dashboard"
                approval_path.write_text(json.dumps(approval, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                STATE.update({"busy": True, "step": "generating approved newsletter", "error": ""})
                def generate():
                    try:
                        issue_number = int(data.get("issue_number") or next_issue_number())
                        issue_date = str(data.get("issue_date") or monday_date().strftime("%d/%m/%Y"))
                        run_command([sys.executable, str(ROOT / "newsletter_workflow.py"), "generate",
                                     "--approval", str(approval_path), "--issue-number", str(issue_number),
                                     "--issue-date", issue_date, "--output-dir", str(ISSUES)], "Newsletter generation")
                        STATE.update({"step": "newsletter ready to preview", "error": ""})
                    except Exception as exc:
                        STATE.update({"step": "generation failed", "error": str(exc)})
                    finally:
                        STATE.update({"busy": False})
                run_in_thread(generate)
                return self.send_json(202, {"status": "generating"})
            return self.send_error(404)
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_json(400, {"error": str(exc)})


def main():
    load_env_file()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"AI Pulse dashboard listening at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
