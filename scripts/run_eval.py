"""Run the labeled eval set through one LLM provider/model and save the scored results.

Uses real API calls (free on the Gemini free tier; a few cents on Claude), so it is run by hand,
never in CI. Provider and model come from the same env vars as the pipeline.

Usage:
    LLM_PROVIDER=gemini LLM_MODEL=gemini-3.5-flash-lite scripts/with-secrets.sh uv run python scripts/run_eval.py
    LLM_PROVIDER=claude scripts/with-secrets.sh uv run python scripts/run_eval.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from signal_scout.enrichment import LlmError, LlmQuotaExhaustedError, SignalInput, llm_client_from_env
from signal_scout.enrichment.evaluation import EvalCase, GoldLabel, score
from signal_scout.enrichment.prompt import PROMPT_VERSION

ROOT = Path(__file__).resolve().parents[1]
LABELS = ROOT / "eval" / "labeled.jsonl"
RESULTS = ROOT / "eval" / "results"
COMPETITORS = ["Asana", "ClickUp", "Notion"]


async def main() -> None:
    records = [json.loads(line) for line in LABELS.read_text().splitlines() if line.strip()]
    client = llm_client_from_env(COMPETITORS)
    sem = asyncio.Semaphore(4)
    cases: list[EvalCase] = []
    stopped: list[str] = []

    async def run_one(rec: dict) -> None:  # type: ignore[type-arg]
        gold = GoldLabel(**rec["label"])
        async with sem:
            if stopped:
                return
            try:
                result = await client.enrich(SignalInput(**rec["signal"]))
            except LlmQuotaExhaustedError as exc:
                stopped.append(str(exc))
                return
            except LlmError as exc:
                print(f"  ! {rec['id']}: {exc}", file=sys.stderr)
                cases.append(EvalCase(rec["id"], gold, None, None))
                return
        cases.append(EvalCase(rec["id"], gold, result.enrichment, result.usage))

    print(f"eval: {len(records)} signals, {client.provider}/{client.model}, prompt {PROMPT_VERSION}", file=sys.stderr)
    await asyncio.gather(*(run_one(r) for r in records))
    if stopped:
        sys.exit(f"stopped: {stopped[0]} ({len(cases)} of {len(records)} done; nothing saved)")

    report = score(cases)
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    out = RESULTS / f"{stamp}-{client.provider}-{client.model}-{PROMPT_VERSION}.json"
    out.write_text(
        json.dumps(
            {
                "generated_at": stamp,
                "provider": client.provider,
                "model": client.model,
                "prompt_version": PROMPT_VERSION,
                "report": asdict(report),
                "cases": [
                    {
                        "id": c.id,
                        "gold": c.gold.model_dump(),
                        "predicted": c.predicted.model_dump() if c.predicted else None,
                        "usage": c.usage.model_dump() if c.usage else None,
                    }
                    for c in sorted(cases, key=lambda c: c.id)
                ],
            },
            indent=1,
        )
    )
    print(json.dumps(asdict(report), indent=1))
    print(f"saved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
