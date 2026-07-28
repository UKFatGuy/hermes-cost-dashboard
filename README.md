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

**Auxiliary & fallback models are included** — the model breakdown shows every provider Hermes routes through:
- Primary models (DeepSeek Flash/Pro)
- Auto/fallback billing (internal Hermes retries and fallbacks)
- Gemini vision tasks
- Copilot fallback (when configured)

Everything with zero cost still shows up so you can see *all* activity, not just paid calls.

No proxy, no API keys, no cloud dependency — purely local.

## Windows System Tray App

A companion app that lives in your Windows system tray:

- **Icon** shows in the notification area
- **Hover tooltip** displays live total cost
- **Right-click menu** shows: Open Dashboard, cost breakdown, Refresh, Quit
- **Auto-refreshes** every 5 minutes

**To use:**
1. Download `hermes-cost-tray.exe` from [Releases](https://github.com/UKFatGuy/hermes-cost-dashboard/releases)
2. Run it (no install needed — single .exe, no dependencies)
3. The icon appears in your system tray
4. Right-click → "Open Dashboard" to see the full web view

Or run `run_tray.bat` from the repo root. The app connects to `https://cost.omoikane.icu` by default. Set `COST_API_URL` env var to change the endpoint.

## Requirements

- Python 3.11+
- Hermes Agent (any version with `state.db` — which is every version)
- FastAPI + Uvicorn (installed automatically)

## License

MIT
