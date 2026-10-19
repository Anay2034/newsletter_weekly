#!/usr/bin/env python3
"""Weekly news-only newsletter: rank candidates, record approval, generate HTML.

Ranking and copy generation use the Gemini API when GEMINI_API_KEY is set, with
an optional OpenAI Responses API fallback. No sending is implemented.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COLLECTED = ROOT / "data" / "collected"
ISSUES = ROOT / "issues"
SLOTS = ("lead_ai", "ai_healthcare", "other_ai", "health_insurance_india_a", "health_insurance_india_b")
SLOT_LABELS = {
    "lead_ai": "Lead AI story",
    "ai_healthcare": "AI in healthcare",
    "other_ai": "Other AI field",
    "health_insurance_india_a": "Health insurance in India 1",
    "health_insurance_india_b": "Health insurance in India 2",
}
RANKING_PROMPT = """You are the editorial researcher for AI Pulse, a weekly internal newsletter for a nontechnical health-insurance audience in India. Return top recommendations plus four alternatives for each of five slots. Choose only supplied IDs, never invent facts, and use each candidate in at most one slot across all recommendations if possible. Provide concise reason, source_quality, caveat, and a confidence label.

Hard rules:
- All items must be published inside the supplied rolling 168-hour window. Reject undated/out-of-window candidates.
- Lead: most significant broadly relevant AI development, not merely a famous company announcement.
- AI in healthcare: one separate healthcare story; prefer credible clinical evidence or concrete deployment and state pilot/study/live stage accurately.
- Other AI: one story outside healthcare.
- Exclude items centered on proposed AI regulation/consultations, AI bans/restrictions, legal uncertainty, fabricated content, AI-enabled fraud/misuse, failures, threats, security risks, or public fear and uncertainty. Do not reframe negative stories positively. Prefer clear constructive research, capability progress, or useful deployments.
- India health-insurance: two distinct, useful stories about the wider market (competitors, IRDAI/government, consumer/claims, premiums/market). Exclude NBHI/Niva Bupa-centered stories. Treat proposals as proposals, not settled rules.
- Cluster duplicate coverage of the same underlying event. An event may appear only once in the options for a slot, even if several publishers covered it. Each alternative must be a different event from the other options in that slot. For the two insurance slots, alternatives should span different market developments (for example policy, insurer performance, products, claims, employer cover) and must not repeat a development already represented in the other insurance slot. If fewer distinct stories exist, return fewer options rather than duplicate headlines. Value independent, authoritative sourcing and useful significance over popularity or repetition.
- The input may contain only a headline and short RSS summary, not the full article. Never invent performance figures, clinical validation, deployment stages, regulatory status, or article details. Base ranking reasons and caveats only on supplied fields. If evidence is limited, lower confidence and say it needs source verification; do not label a story peer-reviewed or official unless the supplied fields support that.
Return JSON with a top-level `slots` object. Its keys are `lead_ai`, `ai_healthcare`, `other_ai`, `health_insurance_india_a`, and `health_insurance_india_b`. Each value is an array of up to five option objects, ordered best first, each with `id`, `reason`, `source_quality`, `caveat`, and `confidence`. Include a short `editorial_notes` string. Return five different underlying events per slot when available; otherwise return fewer and explain.
"""
COPY_PROMPT = """Write copy for the five approved news stories for AI Pulse, a weekly internal newsletter for nontechnical health-insurance employees in India. The fixed email template supplies all layout and styling; return text only and never return HTML. Use only supplied source facts; preserve attribution and uncertainty. Friendly, clear, informative, restrained; no hype or invented implications. Match Issues 3–8: the lead has an opening paragraph, a 'Key highlights ...:' label, exactly three concise bullet lines, each starting with a literal hyphen and space (`- Label: detail`), and a closing paragraph. The renderer turns hyphen lines into styled HTML bullets; do not type bullet glyphs. The four other stories are single compact narrative paragraphs. Do not use labels such as 'What happened:' or 'Why it matters:', do not add generic implications for insurers unless supported, and do not add any AI concept or learning resource. Keep every body inside its character range, including spaces: lead_body 950-1200; each other story body 260-420; titles 15-70; lead_headline 15-70. Do not pad with repetition or unsupported claims; if the supplied facts cannot support the required length, return a concise body and flag that limitation in editorial_notes. Return JSON with fields: issue_number (integer), issue_date (DD/MM/YYYY), lead_headline, lead_body, ai_healthcare_title, ai_healthcare_body, other_ai_title, other_ai_body, health_insurance_a_title, health_insurance_a_body, health_insurance_b_title, health_insurance_b_body, editorial_notes, and emphasis_terms. emphasis_terms must map each body field name to an array of exact phrases present in that body to emphasize, prioritizing organization/product names, named studies, and important figures; do not include phrases absent from the text. No markdown or HTML in body text."""
CONTENT_RESEARCH_PROMPT = """You are the fact-checking editor for AI Pulse, an internal weekly newsletter for nontechnical health-insurance employees in India. Use Google Search to research BOTH supplied, already-selected items in this one request. For the concept, verify the plain-language explanation and facts already present in its supplied record; do not introduce an unrelated product example. For the learning resource, check the official provider page. Prefer primary sources, verify claims, and do not invent details. Write specific, plain-language copy that explains the concept clearly and why the selected learning resource is useful to this nontechnical audience. Avoid generic filler, repeated caveats, hype, technical assumptions, and unsupported claims about price, duration, availability, or prerequisites. Preserve each item's title, ID, provider, and URL; only write the requested copy fields. Return one JSON object with `concept_definition` (299–314 characters including spaces; target 306 exactly, in 2–3 concise sentences), `resource_topic` (a concise, specific section label, 15–45 characters), `resource_what_is_it`, and `resource_recommendation_note`. The complete resource card is assembled exactly as: `What is it? {resource_what_is_it} Recommendation: {resource title} by {provider}. {resource_recommendation_note}` and must be 382–437 characters including spaces. Aim near 400 characters. Count the characters in both assembled fields before responding; revise to fit. Return JSON only."""


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_local_env():
    for env_file in (ROOT / ".env", ROOT / "newsletter.env"):
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def api_json(instructions: str, payload: dict) -> dict:
    load_local_env()
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if gemini_key:
        model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = json.dumps({
            "systemInstruction": {"parts": [{"text": instructions}]},
            "contents": [{"role": "user", "parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }).encode()
        request = urllib.request.Request(endpoint, data=body,
            headers={"x-goog-api-key": gemini_key, "Content-Type": "application/json"}, method="POST")
        provider = "Gemini"
    else:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("Set GEMINI_API_KEY in .env to call the ranking/copy model.")
        body = json.dumps({"model": os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
                           "instructions": instructions,
                           "input": json.dumps(payload, ensure_ascii=False),
                           "text": {"format": {"type": "json_object"}}}).encode()
        request = urllib.request.Request("https://api.openai.com/v1/responses", data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
        provider = "OpenAI"
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1000]
        secret = gemini_key if gemini_key else key
        if secret:
            detail = detail.replace(secret, "[redacted]")
        raise RuntimeError(f"{provider} API error {exc.code}: {detail}") from exc
    if provider == "Gemini":
        try:
            text = "".join(part.get("text", "") for part in result["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, TypeError) as exc:
            reason = result.get("promptFeedback", {}).get("blockReason", "empty or blocked response")
            raise RuntimeError(f"Gemini returned no text ({reason}).") from exc
    else:
        text = result.get("output_text")
        if not text:
            text = "".join(block.get("text", "") for item in result.get("output", [])
                           for block in item.get("content", []) if block.get("type") == "output_text")
    return json.loads(text)


def gemini_search_json(instructions: str, payload: dict):
    """Make one Gemini GenerateContent request with Google Search enabled."""
    load_local_env()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Set GEMINI_API_KEY in .env to research the matched concept and learning resource.")
    model = os.environ.get("GEMINI_RESEARCH_MODEL", "gemini-2.5-flash")
    prompt = instructions + "\n\nINPUT DATA (JSON):\n" + json.dumps(payload, ensure_ascii=False)
    body = json.dumps({"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                       "tools": [{"google_search": {}}]}).encode("utf-8")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    request = urllib.request.Request(endpoint, data=body,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1000].replace(key, "[redacted]")
        raise RuntimeError(f"Gemini web research API error {exc.code}: {detail}") from exc
    try:
        candidate = result["candidates"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Gemini web research returned no candidate text.") from exc
    text = "".join(part.get("text", "") for part in candidate.get("content", {}).get("parts", []))
    grounding = candidate.get("groundingMetadata", {})
    queries = grounding.get("webSearchQueries", [])
    citations = [{"title": chunk.get("web", {}).get("title", ""), "url": chunk.get("web", {}).get("uri", "")}
                 for chunk in grounding.get("groundingChunks", []) if chunk.get("web", {}).get("uri")]
    if not text:
        raise RuntimeError("Gemini web research returned no text.")
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.I)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini web research did not return valid JSON: {text[:500]}") from exc
    return data, {"model": model, "search_queries": queries, "citations": citations}


def gemini_repair_json(payload: dict):
    """One small, non-searching Gemini copy correction if research copy misses limits."""
    load_local_env()
    key = os.environ.get("GEMINI_API_KEY")
    model = os.environ.get("GEMINI_RESEARCH_MODEL", "gemini-2.5-flash")
    instructions = ("Adjust only the supplied concept and learning-resource copy to satisfy the exact character ranges. "
                    "Preserve verified facts and specificity; remove redundancy, never add filler. Keep the resource title "
                    "and provider fixed. The concept_definition must be 299–314 characters including spaces. "
                    "The assembled resource card is exactly `What is it? {resource_what_is_it} Recommendation: "
                    "{title} by {provider}. {resource_recommendation_note}` and must be 382–437 characters including spaces. "
                    "Return JSON with concept_definition, resource_what_is_it, resource_recommendation_note only.")
    body = json.dumps({"systemInstruction": {"parts": [{"text": instructions}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}],
        "generationConfig": {"responseMimeType": "application/json"}}).encode("utf-8")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    request = urllib.request.Request(endpoint, data=body,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1000].replace(key, "[redacted]")
        raise RuntimeError(f"Gemini character-limit correction failed ({exc.code}): {detail}") from exc
    try:
        text = "".join(part.get("text", "") for part in result["candidates"][0]["content"]["parts"])
        return json.loads(text)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Gemini did not return valid JSON for the character-limit correction.") from exc


def research_matched_sections(content_sections):
    concept = content_sections["concept"]
    resource = content_sections["resource"]
    payload = {
        "matched_concept": {key: concept.get(key) for key in ("id", "title", "definition", "semantic_text")},
        "matched_learning_resource": {key: resource.get(key) for key in
            ("id", "title", "topic", "description", "provider", "url", "what_is_it", "recommendation_note", "audience_basis")},
        "audience": "Nontechnical health-insurance employees in India",
    }
    result, research_audit = gemini_search_json(CONTENT_RESEARCH_PROMPT, payload)
    required = ("concept_definition", "resource_topic", "resource_what_is_it", "resource_recommendation_note")
    missing = [key for key in required if not isinstance(result.get(key), str) or not result[key].strip()]
    if missing:
        raise RuntimeError("Gemini web research omitted required copy fields: " + ", ".join(missing))
    def measure(candidate):
        card = (f"What is it? {candidate['resource_what_is_it']} Recommendation: "
                f"{resource['title']} by {resource['provider']}. {candidate['resource_recommendation_note']}")
        return {"concept_definition": len(candidate["concept_definition"]), "resource_card": len(card)}
    counts = measure(result)
    corrected = False
    fallback_fields = []
    if not 299 <= counts["concept_definition"] <= 314 or not 382 <= counts["resource_card"] <= 437:
        fixed = gemini_repair_json({"concept_definition": result["concept_definition"],
            "resource_what_is_it": result["resource_what_is_it"],
            "resource_recommendation_note": result["resource_recommendation_note"],
            "title": resource["title"], "provider": resource["provider"], "measured_characters": counts})
        result.update({key: fixed[key] for key in ("concept_definition", "resource_what_is_it", "resource_recommendation_note")})
        counts = measure(result)
        corrected = True
    if not 299 <= counts["concept_definition"] <= 314:
        existing = concept.get("definition", "")
        if not 299 <= len(existing) <= 314:
            raise RuntimeError("Gemini concept copy missed its character range and no valid bank copy is available: " + str(counts))
        result["concept_definition"] = existing
        fallback_fields.append("concept_definition")
    if not 382 <= counts["resource_card"] <= 437:
        existing_what = resource.get("what_is_it", "")
        existing_note = resource.get("recommendation_note", "")
        existing_card = (f"What is it? {existing_what} Recommendation: {resource['title']} by "
                         f"{resource['provider']}. {existing_note}")
        if not 382 <= len(existing_card) <= 437:
            raise RuntimeError("Gemini resource copy missed its character range and no valid bank copy is available: " + str(counts))
        result["resource_what_is_it"] = existing_what
        result["resource_recommendation_note"] = existing_note
        fallback_fields.extend(["resource_what_is_it", "resource_recommendation_note"])
    counts = measure(result)
    concept["definition"] = result["concept_definition"]
    resource["topic"] = result["resource_topic"]
    resource["what_is_it"] = result["resource_what_is_it"]
    resource["recommendation_note"] = result["resource_recommendation_note"]
    source_urls = [item["url"] for item in research_audit.get("citations", []) if item.get("url")]
    research_audit.update({"character_counts": counts, "character_limit_correction_used": corrected,
                           "bank_copy_fallback_fields": fallback_fields,
                           "resource_card_characters": counts["resource_card"],
                           "source_urls": source_urls})
    return content_sections, research_audit


def gemini_embeddings(texts):
    """Get cached SEMANTIC_SIMILARITY vectors from Gemini in API batches."""
    load_local_env()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Set GEMINI_API_KEY in .env to select this week's concept and resource.")
    model = os.environ.get("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
    cache_path = ROOT / "data" / "embedding_cache.json"
    cache = read_json(cache_path) if cache_path.is_file() else {}
    keys = [hashlib.sha256((model + "\n" + text).encode("utf-8")).hexdigest() for text in texts]
    missing = []
    for cache_key, text in zip(keys, texts):
        if cache_key not in cache:
            missing.append((cache_key, text))
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"
    for offset in range(0, len(missing), 100):
        batch = missing[offset:offset + 100]
        body = json.dumps({"requests": [{
            "model": f"models/{model}",
            "content": {"parts": [{"text": text}]},
            "embedContentConfig": {"taskType": "SEMANTIC_SIMILARITY", "outputDimensionality": 768}
        } for _, text in batch]}).encode("utf-8")
        request = urllib.request.Request(endpoint, data=body,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:1000].replace(key, "[redacted]")
            raise RuntimeError(f"Gemini embedding API error {exc.code}: {detail}") from exc
        vectors = [item.get("values", []) for item in result.get("embeddings", [])]
        if len(vectors) != len(batch) or any(not vector for vector in vectors):
            raise RuntimeError("Gemini returned an incomplete set of similarity embeddings.")
        for (cache_key, _), vector in zip(batch, vectors):
            cache[cache_key] = vector
        write_json(cache_path, cache)
    return [cache[cache_key] for cache_key in keys]


def select_content_sections(selected, copy, issue_key):
    bank = read_json(ROOT / "data" / "content_banks.json")
    history_path = ROOT / "data" / "content_history.json"
    history = read_json(history_path) if history_path.is_file() else {"legacy_used": {}, "used_by_issue": {}}
    history.setdefault("used_by_issue", {})
    if issue_key in history["used_by_issue"]:
        saved = history["used_by_issue"][issue_key]
        concepts = {item["id"]: item for item in bank["concepts"]}
        resources = {item["id"]: item for item in bank["resources"]}
        if saved.get("concept_id") in concepts and saved.get("resource_id") in resources:
            return {"concept": concepts[saved["concept_id"]], "resource": resources[saved["resource_id"]]}, saved.get("similarity", {})

    used_concepts = {item.get("concept_id") for item in history["used_by_issue"].values()}
    used_resources = {item.get("resource_id") for item in history["used_by_issue"].values()}
    legacy = history.get("legacy_used", {})
    legacy_concept_titles = {re.sub(r"[^a-z0-9]", "", x.get("title", "").lower()) for x in legacy.get("concepts", [])}
    legacy_resource_titles = {re.sub(r"[^a-z0-9]", "", x.get("title", "").lower()) for x in legacy.get("resources", [])}
    available_concepts = [x for x in bank["concepts"] if x["id"] not in used_concepts and
                          re.sub(r"[^a-z0-9]", "", x["title"].lower()) not in legacy_concept_titles]
    available_resources = [x for x in bank["resources"] if x.get("audience_level") == "nontechnical_beginner" and
                           x["id"] not in used_resources and
                           re.sub(r"[^a-z0-9]", "", x["title"].lower()) not in legacy_resource_titles]
    if not available_concepts or not available_resources:
        raise RuntimeError("No unused concept or audience-screened beginner learning resource remains in the saved bank.")

    news = []
    for slot in SLOTS:
        item = selected[slot]
        title_key = {"lead_ai":"lead_headline", "ai_healthcare":"ai_healthcare_title", "other_ai":"other_ai_title",
                     "health_insurance_india_a":"health_insurance_a_title", "health_insurance_india_b":"health_insurance_b_title"}[slot]
        body_key = {"lead_ai":"lead_body", "ai_healthcare":"ai_healthcare_body", "other_ai":"other_ai_body",
                    "health_insurance_india_a":"health_insurance_a_body", "health_insurance_india_b":"health_insurance_b_body"}[slot]
        news.append({"slot": slot, "title": copy.get(title_key, item.get("title", "")),
                     "text": copy.get(body_key, "") + " " + item.get("summary", "")})

    candidates = [("concept", x) for x in available_concepts] + [("resource", x) for x in available_resources]
    candidate_texts = [f"{item['title']}. {item.get('semantic_text') or item.get('what_is_it') or item.get('definition') or (item.get('topic','') + '. ' + item.get('description',''))}" for _, item in candidates]
    all_texts = candidate_texts + [f"{item['title']}. {item['text']}" for item in news]
    vectors = gemini_embeddings(all_texts)
    cand_vectors, news_vectors = vectors[:len(candidates)], vectors[len(candidates):]

    def cosine(left, right):
        dot = sum(a * b for a, b in zip(left, right))
        norm = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
        return dot / norm if norm else 0.0

    ranked = {"concept": [], "resource": []}
    for (kind, item), vector in zip(candidates, cand_vectors):
        match_scores = [{"slot": story["slot"], "title": story["title"], "score": round(cosine(vector, story_vector), 6)}
                        for story, story_vector in zip(news, news_vectors)]
        best_match = max(match_scores, key=lambda result: result["score"])
        ranked[kind].append({"item": item, "score": best_match["score"], "match": best_match, "all_news_scores": match_scores})
    for kind in ranked:
        ranked[kind].sort(key=lambda result: result["score"], reverse=True)
    selected_content = {kind: ranked[kind][0]["item"] for kind in ranked}
    audit = {kind: {"selected_id": ranked[kind][0]["item"]["id"], "selected_title": ranked[kind][0]["item"]["title"],
                    "score": ranked[kind][0]["score"], "closest_news": ranked[kind][0]["match"],
                    "compared_with_all_five_news": ranked[kind][0]["all_news_scores"],
                    "unused_candidates_compared": len(ranked[kind])}
             for kind in ranked}
    return selected_content, audit


def in_window(item, cutoff, now):
    raw = item.get("published_at") or item.get("published_date")
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        return cutoff <= dt <= now
    except ValueError:
        return False


def load_candidates(args):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=168)
    pools = []
    for path in args.input:
        doc = read_json(path)
        entries = doc.get("items", doc.get("candidates", []))
        for item in entries:
            if in_window(item, cutoff, now):
                row = dict(item)
                row.setdefault("publisher", row.get("source", ""))
                row.setdefault("published_date", row.get("published_at", "")[:10])
                pools.append(row)
    # URL and normalized-title dedupe. Preserve the richer record on duplicates.
    unique = {}
    for item in pools:
        key = item.get("url", "") or re.sub(r"[^a-z0-9]+", " ", item.get("title", "").lower()).strip()
        old = unique.get(key)
        if not old or len(str(item.get("summary", ""))) > len(str(old.get("summary", ""))):
            unique[key] = item
    return list(unique.values()), cutoff, now


def cmd_rank(args):
    candidates, cutoff, now = load_candidates(args)
    if not candidates:
        raise RuntimeError("No in-window candidates found in the supplied input files.")
    # Keep the request focused but do not drop candidate categories needed for coverage.
    result = api_json(RANKING_PROMPT, {"window_start_utc": cutoff.isoformat(), "window_end_utc": now.isoformat(),
        "rule": "previous 168 hours only", "candidates": candidates})
    result.update({"created_at": now.isoformat(), "window_hours": 168,
                   "window_start_utc": cutoff.isoformat(), "window_end_utc": now.isoformat(),
                   "candidate_count": len(candidates), "candidates": candidates,
                   "approval_status": "pending"})
    write_json(args.output, result)
    print(f"Ranked {len(candidates)} candidates -> {args.output}\nReview recommendations and create approvals with: approve --ranking {args.output}")


def cmd_collect(args):
    ai_out = args.output_dir / "ai_news_latest.json"
    insurance_out = args.output_dir / "health_insurance_india_latest.json"
    common = [sys.executable, str(ROOT / "collect_ai_news.py"), "--window-hours", str(args.window_hours)]
    for category, output in ((None, ai_out), ("health_insurance_india", insurance_out)):
        command = common + ["--output", str(output)]
        if category:
            command += ["--category", category]
        print("$ " + " ".join(command), flush=True)
        status = subprocess.run(command, cwd=ROOT).returncode
        if status:
            raise RuntimeError(f"Collection failed for {category or 'AI'} with exit code {status}")
    print(f"Weekly pools updated in {args.output_dir}")


def cmd_approve(args):
    ranking = read_json(args.ranking)
    options = ranking.get("slots", ranking.get("recommendations", {}))
    candidate_ids = {str(x.get("id")) for x in ranking.get("candidates", [])}
    choices = {}
    # Be tolerant of model outputs using either per-slot list or object with options.
    for slot in SLOTS:
        value = options.get(slot)
        if isinstance(value, dict):
            value = value.get("options", value.get("candidates", []))
        if isinstance(value, str):
            value = [value]
        ids = []
        for row in value or []:
            ids.append(str(row.get("id")) if isinstance(row, dict) else str(row))
        choices[slot] = ids
    approval = {"ranking_file": str(args.ranking.resolve()), "status": "pending", "approved_ids": {},
                "instructions": "Review every slot, edit approved_ids if needed, and set status to approved only after sign-off. Then run generate. The five IDs must be unique."}
    for slot in SLOTS:
        approval["approved_ids"][slot] = choices[slot][0] if choices[slot] else ""
    write_json(args.output, approval)
    print(f"Created editable approval file: {args.output}\nReview candidates in {args.ranking}, edit approved_ids, then run generate --approval {args.output}.")


def validate_approval(approval, ranking):
    ids = [str(approval.get("approved_ids", {}).get(slot, "")).strip() for slot in SLOTS]
    if any(not x for x in ids):
        raise RuntimeError("Approve exactly one candidate for every news slot before generation.")
    if len(set(ids)) != len(ids):
        raise RuntimeError("A candidate cannot be used in more than one final slot.")
    candidates = {str(x.get("id")): x for x in ranking.get("candidates", [])}
    options = ranking.get("slots", {})
    for slot, item_id in zip(SLOTS, ids):
        allowed = options.get(slot, [])
        allowed_ids = {str(x.get("id")) if isinstance(x, dict) else str(x) for x in allowed}
        if allowed_ids and item_id not in allowed_ids:
            raise RuntimeError(f"{item_id} is not on the ranked shortlist for {slot}.")
    missing = set(ids) - candidates.keys()
    if missing:
        raise RuntimeError("Approved IDs are absent from candidate pool: " + ", ".join(sorted(missing)))
    start = datetime.fromisoformat(ranking["window_start_utc"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(ranking["window_end_utc"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    current_cutoff = now - timedelta(hours=168)
    for item_id in ids:
        item = candidates[item_id]
        if not in_window(item, start, end):
            raise RuntimeError(f"Approved story {item_id} is outside the ranking window.")
        if not in_window(item, current_cutoff, now):
            raise RuntimeError(f"Approved story {item_id} has expired from the rolling seven-day window; rerun collection and ranking.")
    return {slot: candidates[item_id] for slot, item_id in zip(SLOTS, ids)}


def esc(text):
    return html.escape(str(text or ""), quote=True)


def story_card(title, body, icon):
    return f'''<td width="48%" valign="top" style="padding:0 12px 22px 12px"><div style="font-size:22px;color:#7c70b3">{icon}</div><h3 style="font: bold 15px Arial,sans-serif;color:#1a1a1a">{esc(title)}</h3><p style="font:13px/1.6 Arial,sans-serif;color:#333">{esc(body)}</p></td>'''


def format_inline(text, emphasis_terms=()):
    """Escape plain copy and add only controlled <strong> emphasis spans."""
    terms = sorted({str(term).strip() for term in emphasis_terms if str(term).strip()}, key=len, reverse=True)
    if not terms:
        return esc(text)
    pattern = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(term) for term in terms) + r")(?![A-Za-z0-9])", re.I)
    output, cursor = [], 0
    for match in pattern.finditer(str(text)):
        output.append(esc(str(text)[cursor:match.start()]))
        output.append("<strong>" + esc(match.group(0)) + "</strong>")
        cursor = match.end()
    output.append(esc(str(text)[cursor:]))
    return "".join(output)


def format_lead_body(text, emphasis_terms=()):
    """Restore the Issues 3–8 lead structure from the generated plain text."""
    lines = str(text).strip().splitlines()
    highlight_index = next((i for i, line in enumerate(lines)
                            if re.match(r"\s*(?:Key highlights|New abilities|Highlights)[^:]*:", line, re.I)), None)
    if highlight_index is None:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", str(text)) if part.strip()]
        return "<br><br>".join(format_inline(part.replace("\n", " "), emphasis_terms) for part in paragraphs)

    before = " ".join(line.strip() for line in lines[:highlight_index] if line.strip())
    label = lines[highlight_index].strip()
    after = lines[highlight_index + 1:]

    def render_list(items):
        html = '<ul style="margin-top:8px; margin-bottom:15px; padding-left:20px;">'
        for i, item in enumerate(items):
            margin = "0" if i == len(items) - 1 else "6px"
            html += f'<li style="margin-bottom:{margin};">{item}</li>'
        return html + "</ul>"

    bullet_start = next((i for i, line in enumerate(after) if re.match(r"\s*[•*-]\s+", line)), None)
    if bullet_start is None:
        # Earlier copies omitted the hyphen prefix but kept one labeled item per line.
        # Recover those lines as bullets so the template never flattens them into prose.
        items, cursor = [], 0
        while cursor < len(after) and not after[cursor].strip():
            cursor += 1
        while cursor < len(after) and len(items) < 3:
            match = re.match(r"\s*([^:\n]{2,60}):\s+(.+)\s*$", after[cursor])
            if not match:
                break
            item = ("<strong>" + esc(match.group(1).strip()) + ":</strong> "
                    + format_inline(match.group(2).strip(), emphasis_terms))
            items.append(item)
            cursor += 1
        if len(items) == 3:
            while cursor < len(after) and not after[cursor].strip():
                cursor += 1
            trailing = " ".join(line.strip() for line in after[cursor:] if line.strip())
            output = format_inline(before, emphasis_terms) + "<br><br>" if before else ""
            output += "<strong>" + esc(label) + "</strong><br><br>" + render_list(items)
            if trailing:
                output += " " + format_inline(trailing, emphasis_terms)
            return output
        return "<br><br>".join(filter(None, [format_inline(before, emphasis_terms),
            "<strong>" + esc(label) + "</strong>",
            format_inline(" ".join(line.strip() for line in after if line.strip()), emphasis_terms)]))
    bullets, trailing = [], []
    in_bullets = True
    for line in after[bullet_start:]:
        match = re.match(r"\s*[•*-]\s+(.*)", line)
        if match and in_bullets:
            content = match.group(1).strip()
            if ":" in content:
                bullet_label, detail = content.split(":", 1)
                rendered = "<strong>" + esc(bullet_label.strip()) + ":</strong> " + format_inline(detail.strip(), emphasis_terms)
            else:
                rendered = format_inline(content, emphasis_terms)
            bullets.append(rendered)
        elif line.strip():
            in_bullets = False
            trailing.append(line.strip())
    rendered_bullets = []
    for i, bullet in enumerate(bullets):
        rendered_bullets.append(bullet)
    list_html = render_list(rendered_bullets)
    output = format_inline(before, emphasis_terms) + "<br><br>" if before else ""
    output += "<strong>" + esc(label) + "</strong>" + list_html
    if trailing:
        output += " " + format_inline(" ".join(trailing), emphasis_terms)
    return output


def render_html(copy, selected, issue_no, content_sections=None):
    # Issue 8 is the fixed, approved email layout. Replace issue metadata, the
    # five selected stories, and the concept/resource card text while preserving layout.
    template_path = ROOT / "letter" / "i8.html"
    if not template_path.is_file():
        raise RuntimeError(f"Newsletter template is missing: {template_path}")
    document = template_path.read_text(encoding="utf-8")
    document = re.sub(r"Issue #\d+\s*&nbsp;\|&nbsp;\s*\d{2}/\d{2}/\d{4}",
                      f"Issue #{issue_no} &nbsp;|&nbsp; {esc(copy['issue_date'])}", document, count=1)

    def find_inner(tag, search_from, style_prefix=None):
        for opening in re.finditer(fr"<{tag}\b[^>]*>", document[search_from:], re.I | re.S):
            start_tag = opening.group(0)
            if style_prefix and style_prefix not in start_tag:
                continue
            absolute_start = search_from + opening.start()
            content_start = search_from + opening.end()
            depth = 1
            tokens = re.compile(fr"</?{tag}\b[^>]*>", re.I | re.S)
            for token in tokens.finditer(document, content_start):
                if token.group(0).startswith("</"):
                    depth -= 1
                    if depth == 0:
                        return content_start, token.start(), absolute_start, token.end()
                elif not token.group(0).rstrip().endswith("/>"):
                    depth += 1
        raise RuntimeError(f"Could not find the {tag} region in the fixed newsletter template.")

    def replace_region(tag, marker, content, style_prefix=None, occurrence=0):
        nonlocal document
        marker_pos = document.index(marker) + len(marker)
        matches = []
        cursor = marker_pos
        for _ in range(occurrence + 1):
            matches.append(find_inner(tag, cursor, style_prefix))
            cursor = matches[-1][3]
        inner_start, inner_end, _, _ = matches[-1]
        document = document[:inner_start] + content + document[inner_end:]

    replace_region("td", "<!-- LEAD HEADLINE -->", esc(copy["lead_headline"]))
    emphasis = copy.get("emphasis_terms", {})
    lead_body = format_lead_body(copy["lead_body"], emphasis.get("lead_body", []))
    replace_region("td", "<!-- LEAD BODY -->", lead_body)
    if content_sections:
        concept = content_sections["concept"]
        resource = content_sections["resource"]
        concept_chars = len(concept.get("definition", ""))
        resource_plain_text = (f'What is it? {resource.get("what_is_it", "")} '
                               f'Recommendation: {resource.get("title", "")} by {resource.get("provider", "")}. '
                               f'{resource.get("recommendation_note", "")}')
        resource_chars = len(resource_plain_text)
        if not 299 <= concept_chars <= 314:
            raise ValueError(f"AI concept explanation is {concept_chars} characters; expected 299–314.")
        if not 382 <= resource_chars <= 437:
            raise ValueError(f"AI learning resource card is {resource_chars} characters; expected 382–437.")
        replace_region("td", "<!-- AI CONCEPT OF THE WEEK -->", esc(concept["title"]) + ":", 'bgcolor="#d7ecd9"')
        replace_region("td", "<!-- AI CONCEPT OF THE WEEK -->", esc(concept["definition"]), 'bgcolor="#dcd3f0"')
        replace_region("td", "<!-- AI LEARNING RESOURCE -->", esc(resource["topic"]) + ":", 'bgcolor="#d4e6f1"')
        what_is_it = resource.get("what_is_it") or f'A short learning resource about {resource["topic"]}.'
        recommendation_note = resource.get("recommendation_note") or resource.get("description", "")
        resource_text = (f'<strong>What is it?</strong> {esc(what_is_it)} '
                         f'<br><br><strong>Recommendation:</strong> <em><a href="{esc(resource["url"])}" '
                         f'style="color:#333333; text-decoration:underline;">{esc(resource["title"])}</a></em> '
                         f'by {esc(resource["provider"])}. {esc(recommendation_note)}')
        replace_region("td", "<!-- AI LEARNING RESOURCE -->", resource_text, 'bgcolor="#eaf2f8"')

    news = [
        ("<!-- OTHER AI NEWS -->", "ai_healthcare_title", "ai_healthcare_body"),
        ("<!-- OTHER AI NEWS -->", "other_ai_title", "other_ai_body"),
        ("<!-- HEALTH INSURANCE IN INDIA -->", "health_insurance_a_title", "health_insurance_a_body"),
        ("<!-- HEALTH INSURANCE IN INDIA -->", "health_insurance_b_title", "health_insurance_b_body"),
    ]
    seen_per_section = {}
    for marker, title_key, body_key in news:
        # Each section has two title cells and two copy blocks. Restrict lookup
        # to the relevant section so the concept/resource text stays untouched.
        section_start = document.index(marker) + len(marker)
        next_marker = document.find("<!--", section_start)
        while next_marker != -1 and "OTHER AI NEWS" not in document[next_marker:next_marker + 60].upper() and "HEALTH INSURANCE IN INDIA" not in document[next_marker:next_marker + 70].upper() and "AI LEARNING RESOURCE" not in document[next_marker:next_marker + 70].upper() and "FOOTER" not in document[next_marker:next_marker + 40].upper():
            next_marker = document.find("<!--", next_marker + 4)
        section_end = next_marker if next_marker != -1 else len(document)
        section = document[section_start:section_end]
        title_matches = list(re.finditer(r'<td\b(?=[^>]*style="padding-left:10px; font-family:Arial, sans-serif; font-size:13px; font-weight:bold)[^>]*>', section, re.I | re.S))
        body_matches = list(re.finditer(r'<div\b(?=[^>]*style="font-family:Arial, sans-serif; font-size:12px; color:#333333;)[^>]*>', section, re.I | re.S))
        occurrence = seen_per_section.get(marker, 0)
        seen_per_section[marker] = occurrence + 1
        if len(title_matches) <= occurrence or len(body_matches) <= occurrence:
            raise RuntimeError(f"Could not find a story card in template section {marker}.")
        title_open, body_open = title_matches[occurrence], body_matches[occurrence]
        title_start = section_start + title_open.end()
        title_end = document.index("</td>", title_start)
        body_start = section_start + body_open.end()
        body_end = document.index("</div>", body_start)
        document = (document[:title_start] + esc(copy[title_key]) + document[title_end:body_start]
                    + format_inline(copy[body_key], emphasis.get(body_key, [])) + document[body_end:])

    return document


def cmd_generate(args):
    try:
        datetime.strptime(args.issue_date, "%d/%m/%Y")
    except ValueError as exc:
        raise RuntimeError("Issue date must use DD/MM/YYYY format.") from exc
    if args.issue_number < 1:
        raise RuntimeError("Issue number must be a positive integer.")
    approval = read_json(args.approval)
    if approval.get("status") != "approved":
        raise RuntimeError("Approval is still pending. Set status to 'approved' in the approval JSON after editorial sign-off.")
    ranking_path = Path(approval.get("ranking_file", args.ranking or ""))
    if not ranking_path.is_absolute():
        ranking_path = (args.approval.parent / ranking_path).resolve()
    if not ranking_path.exists():
        raise RuntimeError("Cannot find ranking file. Set ranking_file in approval JSON or use --ranking.")
    ranking = read_json(ranking_path)
    selected = validate_approval(approval, ranking)
    copy = api_json(COPY_PROMPT, {"issue_number": args.issue_number, "issue_date": args.issue_date,
        "stories": {slot: {k: v for k, v in item.items() if k in ("title", "summary", "url", "publisher", "source", "published_date", "published_at")}
                    for slot, item in selected.items()}})
    copy["issue_number"] = args.issue_number
    copy["issue_date"] = args.issue_date
    limits = {"lead_headline":(15,70),"lead_body":(950,1200),"ai_healthcare_title":(15,70),"ai_healthcare_body":(260,420),
              "other_ai_title":(15,70),"other_ai_body":(260,420),"health_insurance_a_title":(15,70),"health_insurance_a_body":(260,420),
              "health_insurance_b_title":(15,70),"health_insurance_b_body":(260,420)}
    out_of_range = {key: {"characters": len(str(copy.get(key, ""))), "required": [minimum, maximum]}
                    for key, (minimum, maximum) in limits.items()
                    if not copy.get(key) or not minimum <= len(str(copy[key])) <= maximum}
    if out_of_range:
        raise RuntimeError("Generated copy is missing or outside its character range: " + json.dumps(out_of_range))
    issue_key = datetime.strptime(args.issue_date, "%d/%m/%Y").date().isoformat()
    content_sections, content_similarity = select_content_sections(selected, copy, issue_key)
    content_sections, content_research = research_matched_sections(content_sections)
    slug = args.issue_date.replace("/", "-")
    json_path, html_path = args.output_dir / f"issue_{slug}.json", args.output_dir / f"issue_{slug}.html"
    issue = {"issue_number": args.issue_number, "issue_date": args.issue_date,
             "approved_ids": approval["approved_ids"], "stories": selected, "copy": copy,
             "content_sections": content_sections, "content_similarity": content_similarity,
             "content_research": content_research}
    write_json(json_path, issue)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(copy, selected, args.issue_number, content_sections), encoding="utf-8")
    history_path = ROOT / "data" / "content_history.json"
    history = read_json(history_path) if history_path.is_file() else {"legacy_used": {}, "used_by_issue": {}}
    history.setdefault("used_by_issue", {})[issue_key] = {
        "concept_id": content_sections["concept"]["id"],
        "resource_id": content_sections["resource"]["id"],
        "concept_title": content_sections["concept"]["title"],
        "resource_title": content_sections["resource"]["title"],
        "similarity": content_similarity,
    }
    write_json(history_path, history)
    approval["content_section_ids"] = {"concept": content_sections["concept"]["id"], "resource": content_sections["resource"]["id"]}
    approval["status"] = "generated"
    approval["generated_html"] = str(html_path)
    write_json(args.approval, approval)
    print(f"Generated approved issue: {html_path}\nStructured copy: {json_path}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="Update both rolling seven-day news pools")
    collect.add_argument("--window-hours", type=int, default=168)
    collect.add_argument("--output-dir", type=Path, default=COLLECTED)
    collect.set_defaults(func=cmd_collect)
    rank = sub.add_parser("rank", help="AI-rank collected candidates into a recommendation shortlist")
    rank.add_argument("--input", type=Path, nargs="+", required=True)
    rank.add_argument("--output", type=Path, default=ISSUES / "ranking.json")
    rank.set_defaults(func=cmd_rank)
    approve = sub.add_parser("approve", help="Create editable approval JSON from ranking")
    approve.add_argument("--ranking", type=Path, required=True)
    approve.add_argument("--output", type=Path, default=ISSUES / "approval.json")
    approve.set_defaults(func=cmd_approve)
    gen = sub.add_parser("generate", help="Generate HTML and structured copy after human approval")
    gen.add_argument("--approval", type=Path, required=True)
    gen.add_argument("--ranking", type=Path)
    gen.add_argument("--issue-number", type=int, required=True)
    gen.add_argument("--issue-date", required=True, help="DD/MM/YYYY")
    gen.add_argument("--output-dir", type=Path, default=ISSUES)
    gen.set_defaults(func=cmd_generate)
    args = p.parse_args()
    try:
        args.func(args)
    except (RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
