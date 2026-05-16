# Changelog

## 0.1.0 — 2026-04-19

Initial release of the multiplai plugin.

- Repository scaffold with plugin.json, marketplace.json, hooks.json
- Path resolver with environment variable cascade and standalone fallbacks
- Model client abstraction with Agent SDK and Anthropic API fallback
- Venv bootstrap on SessionStart hook
- Context router for UserPromptSubmit hook
- Session lifecycle hooks (start, stop, end)
- Extract learnings on Stop hook
- AutoDream consolidation
- Synthesize now script
- Generate catalog script
- Pre-compact hook
- Memory templates (me.md, technical-pref.md, preferences.md)
- Skills: setup, dream, health
