# Signal Scout: AI Competitor Insights Pipeline

Watches competitors' news, press releases, and website changes, uses an LLM (Claude or Gemini) to turn them into ranked insights, and serves them over search, Slack alerts, and an MCP server for AI agents.

> **Status: early development (Sprint 1, prototype).** Numbers will appear here only after they are measured, with evidence in `docs/results/`.

**LLM provider:** Claude adapter built and tested; production run on Gemini.

## Swapping providers

Enrichment goes through one `LLMClient` interface with two real adapters (Gemini via `google-genai`, Claude via `anthropic`). Both return the same validated `Enrichment`, so switching is one environment variable:

```sh
LLM_PROVIDER=claude   # or gemini (default); optionally LLM_MODEL=<model id>
```

## License

MIT
