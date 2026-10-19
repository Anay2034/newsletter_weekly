#!/usr/bin/env python3
"""Install a per-user macOS LaunchAgent to open the approval dashboard Mondays at 9am."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE_LABEL = "com.aipulse.newsletter-dashboard"
WEEKLY_LABEL = BASE_LABEL + ".weekly"
AGENT_DIR = Path.home() / "Library" / "LaunchAgents"
WEEKLY_PLIST = AGENT_DIR / f"{WEEKLY_LABEL}.plist"
OLD_PLIST = AGENT_DIR / f"{BASE_LABEL}.plist"
LAUNCHCTL = "/bin/launchctl"


def main():
    if sys.platform != "darwin":
        raise SystemExit("The Monday scheduler installer requires macOS.")
    if not (ROOT / ".venv" / "bin" / "python").exists():
        raise SystemExit("Project virtual environment is missing. Create .venv first.")
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    weekly_config = {
        "Label": WEEKLY_LABEL,
        "ProgramArguments": ["/usr/bin/open", "-a", "Terminal", str(ROOT / "open_newsletter_dashboard.command")],
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": {"Weekday": 1, "Hour": 9, "Minute": 15},
        "RunAtLoad": False,
        "StandardOutPath": str(ROOT / "issues" / "launch-agent.log"),
        "StandardErrorPath": str(ROOT / "issues" / "launch-agent.log"),
    }
    WEEKLY_PLIST.write_bytes(plistlib.dumps(weekly_config))
    os.chmod(WEEKLY_PLIST, 0o600)
    domain = f"gui/{os.getuid()}"
    for label in (BASE_LABEL, BASE_LABEL + ".server", WEEKLY_LABEL):
        subprocess.run([LAUNCHCTL, "bootout", f"{domain}/{label}"], check=False, capture_output=True)
    for old_plist in (OLD_PLIST, AGENT_DIR / f"{BASE_LABEL}.server.plist"):
        if old_plist.exists():
            old_plist.unlink()
    result = subprocess.run([LAUNCHCTL, "bootstrap", domain, str(WEEKLY_PLIST)], capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f"Could not register {WEEKLY_PLIST.name}: {result.stderr.strip()}")
    print(f"Installed {WEEKLY_PLIST}")
    print("Every Monday at 9:15 AM local time, macOS opens Terminal and starts the local dashboard. If no ranking exists for that week, the page automatically collects and ranks news.")
    print("To remove it: launchctl bootout " + domain + "/" + WEEKLY_LABEL + " && rm " + str(WEEKLY_PLIST))


if __name__ == "__main__":
    main()
