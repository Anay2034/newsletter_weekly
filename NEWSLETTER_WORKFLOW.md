# AI Pulse weekly news workflow

This process prepares five news stories, then selects one unused AI concept and one unused learning resource from saved sample banks using Gemini semantic similarity. It generates a reviewable newsletter HTML file and does not send it.

## Weekly sequence

1. **Collect throughout the week.** Run this once each day so the rolling pool keeps stories discovered earlier in the week:

   ```sh
   .venv/bin/python newsletter_workflow.py collect
   ```

   This updates `data/collected/ai_news_latest.json` and `data/collected/health_insurance_india_latest.json`. Both are filtered to the previous 168 hours by publication time. The collector needs outbound DNS/HTTPS access.

2. **Rank on newsletter day.** Put `GEMINI_API_KEY` in the project `.env` (the local dashboard reads it directly), or export it in the environment, then run:

   ```sh
   .venv/bin/python newsletter_workflow.py rank \
     --input data/collected/ai_news_latest.json data/collected/health_insurance_india_latest.json \
     --output issues/ranking_YYYY-Www.json
   ```

   Ranking and news copy generation use Gemini `gemini-3.8-flash` by default; set `GEMINI_MODEL` to override it. The concept/resource Google Search step uses `gemini-2.5-flash` by default; set `GEMINI_RESEARCH_MODEL` to override it. `OPENAI_API_KEY` remains a fallback only when no Gemini key is configured for news ranking/copy. Ranking returns up to five choices for each slot: lead AI, AI in healthcare, AI outside healthcare, and two distinct India health-insurance market stories. It removes out-of-window and duplicate-URL entries before ranking. Editorial rules exclude AI fear/security-risk angles and NBHI-centered insurance items.

3. **Prepare the approval sheet.**

   ```sh
   .venv/bin/python newsletter_workflow.py approve \
     --ranking issues/ranking_YYYY-Www.json \
     --output issues/approval_YYYY-Www.json
   ```

   The dashboard preselects one recommendation per slot. Review its linked candidate and alternatives, or choose “Add news manually…” at the bottom of any slot dropdown and provide the headline, publisher, source URL, publication date/time, and summary. Manual items must be within the rolling seven-day window and are marked for source verification. After news approval, unused concept/resource candidates are matched to the five stories by Gemini semantic similarity. One combined Gemini request then uses Google Search to research both winners and create audience-specific copy, grounded in the matched story and the resource provider's page. The copy is checked against the fixed character ranges before the existing Issue 8 template is rendered. Previously used IDs and legacy items from Issues 1–8 are removed from the competition. Nothing is sent.

4. **Generate the approved issue.** After approval, run:

   ```sh
   .venv/bin/python newsletter_workflow.py generate \
     --approval issues/approval_YYYY-Www.json \
     --issue-number 9 --issue-date 12/10/2026
   ```

   The command writes structured copy and HTML from `letter/i8.html`, keeping its layout while replacing issue metadata, the five news stories, and the concept/resource cards. It chooses the highest Gemini semantic-similarity match from each unused bank in `data/content_banks.json`; resources must first be screened as beginner material for a nontechnical audience. Each candidate is compared with each of the five selected news stories, and the candidate's best story similarity determines its rank. One Gemini request with Google Search then researches the matched concept and resource together. The call receives only those two bank records; it checks the concept explanation and verifies the learning resource against the provider's page. If the generated copy misses its character range, one small no-search correction is attempted; if the concept still misses, the valid bank text is retained and recorded in the issue audit. The issue JSON records search queries, citations, fallbacks, and character counts. Used IDs are recorded in `data/content_history.json` only after generation; the eight concepts and resources from Issues 1–8 are excluded. Copy ranges, including spaces: concept explanation 299–314 characters; the complete learning-resource card text 382–437; lead headline 15–70 and lead body 950–1,200; each supporting title 15–70 and body 260–420. Generation rejects concept/resource copy outside its ranges. The renderer restores the Issues 3–8 lead list and emphasis, bold emphasis in story cards, and the italicized resource link in code while preserving the template HTML. At present, two resources are audience-screened; the remaining bank entries are excluded until reviewed. Sending is not implemented.

## Approval boundary

Ranking is advisory. The generated HTML is created only after a human sets the approval file to `approved`; changing a story requires regenerating from the updated approval. Check that the issue copy preserves proposal status, study/deployment stage, attribution, and source caveats before using it.

## Current approval packet

`issues/ranking_2026-W41.json` and `issues/approval_2026-W41.json` contain the current Gemini-ranked packet. Gemini ranked 476 candidates and returned five options per slot. The selected recommendations are OpenAI's GPT-6 Sol and Luna announcement, Houston Methodist's AI scheduling deployment, Indian Railways' AI deployment, the IRDAI distribution-reform update, and Care Health's premium growth. The packet is pending approval in the dashboard.

## Local dashboard and Monday schedule

The dashboard is served only on this Mac at `http://127.0.0.1:8765`; it is not published. Use **Approve and generate preview** to approve five distinct candidates and create the issue. The page shows a live HTML preview and a download button.

To start it manually, double-click `open_newsletter_dashboard.command`. To launch it every Monday at 9:15 AM in the Mac's local timezone and automatically collect/rank when that week's shortlist does not exist, run:

```sh
.venv/bin/python install_monday_schedule.py
```

The installer creates a per-user macOS LaunchAgent that opens Terminal on Monday at 9:15 AM and runs the local dashboard under your desktop session. Leave that Terminal window open while using the dashboard; closing it stops the local server. Change the `Hour` and `Minute` values in the installer before reinstalling if a different time is preferred. To remove the schedule, run the `launchctl bootout ...` command printed by the installer and delete its plist file.

For unattended ranking and copy generation, the project `.env` file contains `GEMINI_API_KEY`. The dashboard and CLI reload it from disk; `.env` is ignored by Git. A blank example is in `.env.example`. Without the key, collection can run but the AI ranking step will show an actionable error in the dashboard.
