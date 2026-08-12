"""Thin entry point so `uvicorn main:app` (and the verify recipe) can boot the app.

The canonical run path is `python -m hermes_cost_dashboard.main` (see the
systemd unit in deploy/); this shim exists for uvicorn-style boots.
"""

from hermes_cost_dashboard.main import app

if __name__ == "__main__":
    from hermes_cost_dashboard.main import run

    run()
