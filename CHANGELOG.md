# Changelog

All notable changes to this project are documented in this file.

## Unreleased

- Added optional LiteLLM-powered task extraction for messages from `rules.high_priority_senders`.
- Added markdown todo workflow under `MAILTRIAGE_TODO_ROOT` with a cumulative `running.md` and done archive files at `done/YYYY/MM/DD.md`.
- Added automatic migration of checked items (`- [x]` / `- [X]`) from `running.md` to the dated done archive on the next run.
- Preserved completed markdown lines (including user-added notes/tags) when archiving done items.
- Added `.env.example` and README documentation for LiteLLM todo environment variables and behavior.
- Added verbose debug logging for todo sync and LiteLLM request/parse outcomes when `MAILTRIAGE_DEBUG=1`.
- Added optional TLS controls for enterprise certificate chains: `MAILTRIAGE_LITELLM_CA_BUNDLE` and `MAILTRIAGE_LITELLM_INSECURE_SKIP_VERIFY`.
- Switched todo markdown output from checkbox format to plain bullets and now archive done items marked with `DONE:`.
- Added shared dotenv loading so CLI commands also read `.env` automatically (while keeping exported env vars authoritative).
- Tightened LiteLLM todo prompting to reduce timeouts and improved JSON parsing fallbacks (including reasoning content recovery when needed).
- Updated LLM prompt strategy to produce summary+action entries and include explicit `Action: No action required` lines when applicable.
- Added subject-based fallback todo entries when the model returns no parseable tasks, with incident terms mapped to investigation actions.
- Updated `rules.high_priority_senders` to support `email` + optional `name_regex` entries, enabling per-sender display-name matching (e.g., only `via RT` variants).
- Updated ingestion to persist sender display names (`"Name <email>"`) when available and backfill existing rows on re-ingest, enabling reliable name-regex filtering like `via RT`.
- Added debug visibility when high-priority thread capping drops threads and filtered placeholder-style LLM outputs like `<summary>` / `<todo ...>`.
