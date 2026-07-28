# Hermes Cost Dashboard

A live, self-hosted cost dashboard for [Hermes Agent](https://hermes-agent.nousresearch.com). Track your LLM API spending by profile, model, and time period — all from a beautiful dark-themed web UI that runs on your desktop.

![screenshot](https://img.shields.io/badge/status-beta-brightgreen)

## Why?

Hermes tracks token usage and estimated costs in its SQLite database (`state.db`), but there's no built-in dashboard to visualize it. This tool reads that data and gives you:

- **Per-profile costs** — how much did Izzy, Billy, and I spend?
- **Per-model breakdown** — DeepSeek Flash vs Pro vs Gemini
- **Cost over time** — daily/weekly trends
- **Live session table** — recent activity with costs
- **Auto-refresh** — updates every 30 seconds

## Quick Start

```bash
# Install
pip install hermes-cost-dashboard

# Or run from source
git clone https://github.com/UKFatGuy/hermes-cost-dashboard.git
cd hermes-cost-dashboard
pip install -e .

# Launch (reads your Hermes state.db automatically)
hermes-cost-dashboard
# → Open http://localhost:8080
```

## How It Works

The dashboard reads directly from Hermes' `state.db` (read-only, never writes). It queries the `sessions` and `session_model_usage` tables where Hermes already stores `estimated_cost_usd` and `actual_cost_usd` from each API call.

No proxy, no API keys, no cloud dependency — purely local.

## Requirements

- Python 3.11+
- Hermes Agent (any version with `state.db` — which is every version)
- FastAPI + Uvicorn (installed automatically)

## License

MIT
