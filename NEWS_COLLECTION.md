# AI news collection

## Run

Create or use the project virtual environment, then run:

```sh
.venv/bin/python collect_ai_news.py
```

The collector reads `news_sources.json`, pulls configured RSS feeds and Google News search feeds, keeps only items published in the requested window, deduplicates URLs, and merges eligible items into its output. Google News query windows follow `--window-hours`. Run it once a day to build the rolling weekly pool. The store retains the first discovery time and expires stories when they leave the date window. It decodes gzip-compressed feeds, including Google DeepMind's.

The script uses Python's standard library, so the virtual environment needs no extra packages. Feed publication dates without a timezone are rejected because they cannot safely pass the strict time cutoff. The run environment needs outbound DNS/HTTPS access; a sandbox that blocks outbound DNS will fail every source even when the URLs are valid.

For a two-week audit, use `--window-hours 336` and a separate `--output` path so the regular weekly pool is not changed.

Collect the India health-insurance lane alone with `--category health_insurance_india`. The Google News queries exclude the phrase “Niva Bupa”; the editor review should also reject any result whose main subject is NBHI.

## Categories

- `lead_ai`: broad AI candidates that could merit the lead position.
- `ai_healthcare`: AI applications, evidence, policy, or deployments in healthcare.
- `other_ai`: AI developments outside healthcare.
- `health_insurance_india`: Indian health-insurance market, insurer, customer, and regulatory developments, excluding NBHI-specific stories.

The weekly approval view should offer five choices per slot as a starting point, allow a story to be used in only one final slot, and show its source and publication date. An AI ranking step should review the full candidate pool before the approval view; Google News result count and source volume are not proxies for importance.

Apply the editorial fit filter before ranking: exclude stories whose main framing presents AI as a security threat, focuses on unsafe behavior or failure, or mainly creates fear and uncertainty about AI. Prefer useful deployments, constructive capability progress, and positive healthcare or business applications. Keep factual caveats needed for accuracy, but don't turn them into a fear-based angle.

Rank for this newsletter's nontechnical health-insurance audience. For the lead, prioritize major, broadly meaningful AI developments with clear evidence and substantial significance; a frontier research milestone can beat a product launch even when it is not directly about insurance. OpenAI's release of 722 maths manuscripts grouped into 372 result families is eligible for the lead because of its scale, novelty, and broad relevance—not merely because OpenAI published it. For the other AI slots, enforce the coverage mix: one healthcare AI story and one story from another AI field. For healthcare, prefer concrete deployments or credible clinical evidence and accurately describe the stage (pilot, feasibility study, or live service). For India health insurance, select two distinct stories about the wider market: competitor strategy/products/partnerships, IRDAI or government policy, consumer/claims changes, and market or premium data. Exclude stories centered on Niva Bupa/NBHI; the section is for watching the market around NBHI, not reporting on NBHI itself.

AI ranking should return a top recommendation and four alternatives per slot, with concise reasons, source-quality notes, date-window check, and caveats. Exclude duplicates across final slots. The ranking is advisory: an editor approves or replaces candidates, then the approved choices feed letter generation. Sending is out of scope.

## First collection sample

`data/collected/ai_news_research_2026-10-11.json` is a one-time research sample for Oct 4–11, 2026 assembled through web search and direct source checks. The live RSS collector was subsequently run with outbound access; see `data/collected/ai_news_latest.json` for the current rolling pool and per-feed status.

## Recommendation and quality

The collector gathers and dates candidates; it does not itself call an AI model. A separate AI review step should rank candidates using the criteria above and flag caveats. The collection process does not send the newsletter.

## Weekly approval and generation

See [NEWSLETTER_WORKFLOW.md](NEWSLETTER_WORKFLOW.md) for the runnable collect → rank → approve → generate process. Ranking and copy generation use the OpenAI API. The first current-week approval packet is in `issues/` and awaits editorial sign-off.
