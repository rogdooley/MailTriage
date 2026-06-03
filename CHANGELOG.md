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
