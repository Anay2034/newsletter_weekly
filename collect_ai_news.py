#!/usr/bin/env python3
"""Collect recent AI news candidates from configured RSS feeds."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import quote_plus


ROOT = Path(__file__).resolve().parent
USER_AGENT = "NewsletterWeeklyNewsCollector/0.1 (RSS reader)"


def clean(value: str | None) -> str:
    value = unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def node_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return clean("".join(node.itertext()))


def parse_date(value: str) -> datetime | None:
    value = value.strip()
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def child_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for child in node.iter():
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in names and child.text:
            return clean(child.text)
    return ""


def item_link(item: ET.Element) -> str:
    for child in item:
        if child.tag.rsplit("}", 1)[-1].lower() == "link":
            href = child.attrib.get("href")
            if href:
                return href
            if child.text:
                return child.text.strip()
    return ""


def parse_feed(xml_bytes: bytes, source: dict, now: datetime, cutoff: datetime):
    root = ET.fromstring(xml_bytes)
    entries = [element for element in root.iter() if element.tag.rsplit("}", 1)[-1].lower() in ("item", "entry")]
    results = []
    for entry in entries:
        title = child_text(entry, ("title",))
        url = item_link(entry)
        date_text = child_text(entry, ("published", "pubdate", "updated", "date"))
        published = parse_date(date_text) if date_text else None
        if not title or not url or not published or not (cutoff <= published <= now):
            continue
        summary = child_text(entry, ("description", "summary", "content"))
        digest = hashlib.sha256(url.split("?")[0].rstrip("/").lower().encode()).hexdigest()[:16]
        results.append({
            "id": digest,
            "title": title,
            "url": url,
            "published_at": published.isoformat(),
            "discovered_at": now.isoformat(),
            "source": source["name"],
            "category": source["category"],
            "summary": summary,
        })
    return results


def configured_sources(config: dict, window_hours: int):
    sources = list(config.get("feeds", []))
    window_days = max(1, (window_hours + 23) // 24)
    for category, queries in config.get("queries", {}).items():
        for query in queries:
            url = "https://news.google.com/rss/search?q=" + quote_plus(query + f" when:{window_days}d") + "&hl=en-IN&gl=IN&ceid=IN%3Aen"
            sources.append({"name": "Google News: " + query, "url": url, "category": category})
    return sources


def fetch(source: dict, timeout: int) -> bytes:
    request = urllib.request.Request(
        source["url"],
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        encoding = response.headers.get("Content-Encoding", "").lower().strip()
        if encoding == "gzip":
            body = gzip.decompress(body)
        return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "news_sources.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data/collected/ai_news_latest.json")
    parser.add_argument("--window-hours", type=int, default=None)
    parser.add_argument("--category", help="Collect only one configured category")
    parser.add_argument("--timeout", type=int, default=15)
    args = parser.parse_args()

    config = json.loads(args.config.read_text())
    hours = args.window_hours or int(config.get("window_hours", 168))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    collected, source_status = [], []
    sources = configured_sources(config, hours)
    if args.category:
        sources = [source for source in sources if source["category"] == args.category]
        if not sources:
            parser.error(f"Unknown or unconfigured category: {args.category}")
    for source in sources:
        try:
            items = parse_feed(fetch(source, args.timeout), source, now, cutoff)
            collected.extend(items)
            source_status.append({"source": source["name"], "status": "ok", "eligible_items": len(items)})
        except (urllib.error.URLError, TimeoutError, ET.ParseError, OSError) as exc:
            source_status.append({"source": source["name"], "status": "error", "error": str(exc)})

    previous = []
    if args.output.exists():
        try:
            previous = json.loads(args.output.read_text()).get("items", [])
        except (OSError, json.JSONDecodeError):
            previous = []
    unique = {}
    for item in previous + collected:
        old_date = parse_date(item.get("published_at", ""))
        if not old_date or not (cutoff <= old_date <= now):
            continue
        old = unique.get(item["id"])
        if old:
            old_sources = old.get("also_found_by", [old["source"]])
            item_sources = item.get("also_found_by", [item["source"]])
            old["also_found_by"] = sorted(set(old_sources + item_sources))
            if len(item["summary"]) > len(old["summary"]):
                old["summary"] = item["summary"]
            if item.get("discovered_at", "") < old.get("discovered_at", ""):
                old["discovered_at"] = item["discovered_at"]
        else:
            unique[item["id"]] = item
    items = sorted(unique.values(), key=lambda item: item["published_at"], reverse=True)
    result = {
        "collected_at": now.isoformat(),
        "timezone": "UTC (display in Asia/Kolkata)",
        "window_hours": hours,
        "cutoff": cutoff.isoformat(),
        "categories": sorted({source["category"] for source in sources}),
        "source_status": source_status,
        "items": items,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    counts = {}
    for item in items:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    print(json.dumps({"output": str(args.output), "items": len(items), "by_category": counts,
                      "sources_ok": sum(s["status"] == "ok" for s in source_status),
                      "sources_failed": sum(s["status"] == "error" for s in source_status),
                      "previous_items_retained": len(previous)}, indent=2))
    if not items and all(s["status"] == "error" for s in source_status):
        print("All feeds failed; check network access and source URLs.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
