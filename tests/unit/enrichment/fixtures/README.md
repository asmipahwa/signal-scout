# Enrichment adapter fixtures

API responses the adapter tests replay, so tests never call a real provider.

**Provenance (be honest about this):** every file here is currently *hand-built* in the exact
response shape of the official SDKs (`anthropic.types.Message`, `google.genai.types.GenerateContentResponse`,
and the Gemini 429 error body), not captured from a live call. The tests load them through the SDKs' own
models, so a shape mismatch fails loudly.

`gemini_503_overloaded.json` is the exact error body the live API returned on 2026-10-31.

The signal they describe (an Asana outage) comes from a real Bing News item fetched on 2026-10-28; the
enrichment text is written by hand.

The hand-built files stay as they are, because the logic tests assert exact token counts and values.
`scripts/record_llm_fixture.py <provider>` adds `<provider>_recorded.json`, captured from a live call
for the same signal; a contract test replays it through the real adapter (skipped until it exists).
