# LLM enrichment eval

`labeled.jsonl` holds **101 real signals** (from the 2026-10-31 prototype run) with gold labels for the
four things the product depends on: is it really about a tracked competitor, which competitor, which
category, and how important (1–5, using the rubric in the enrichment prompt).

Run it against any provider/model (real API calls, so by hand only, never in CI):

```sh
LLM_PROVIDER=gemini LLM_MODEL=gemini-3.5-flash-lite scripts/with-secrets.sh uv run python scripts/run_eval.py
LLM_PROVIDER=claude scripts/with-secrets.sh uv run python scripts/run_eval.py
```

Results are saved to `eval/results/` and summarized in [`docs/EVALS.md`](../docs/EVALS.md).

## How the labels were made (read before trusting the numbers)

- **Drafted by Claude Code, not yet reviewed by a human.** Every record has `"reviewed_by_human": false`.
  Asmi reviews and corrects them; flip the flag per record when done.
- Labels were written from the same title + snippet the model sees, using the prompt's rubric.
- **Not fully blind:** the labeler had already seen the prototype model's output for roughly 40 of these
  signals (the top-ranked list and the not-relevant list). Expect some bias toward agreeing with
  `gemini-3.5-flash-lite` on those.
- Borderline calls carry a `note` (e.g. Asana as one of many tools in a Cloudflare integration).
- 20 Notion changelog items have **no body text** (Notion's RSS feed ships titles only), so both the
  labeler and the model judged them from the title alone.

Composition: 66 relevant (feature_update 21, outage_or_incident 12, product_launch 10, other 8,
pricing_change 4, acquisition 4, hiring_signal 4, leadership_change 2, partnership 1) and 35 not relevant.
