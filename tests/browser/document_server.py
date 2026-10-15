"""Isolated E2E fixture; never import this server into the production app.

One synthetic marker injects a report-creation fault. No model is constructed.
All uploaded data and the SQLite file must be temporary test data only.
"""

import os
from types import SimpleNamespace

import uvicorn
from os_server.app import create_app

from os_server import documents


def main() -> None:
    if os.environ.get("CELA_E2E_TEST_MODE") != "1":
        raise SystemExit("Explicit CELA_E2E_TEST_MODE=1 is required")
    original = documents.render_report

    def report(state):
        if state.metadata.get("source") == "MVP0_SYNTHETIC_FAILURE":
            raise ValueError("synthetic report creation failure")
        return original(state)

    documents.render_report = report
    # ApiRuntime eagerly requests a platform. This inert fixture supplies only
    # ledger bookkeeping; accidentally running an agent cannot call any model.
    app = create_app(
        lambda mode, max_steps: SimpleNamespace(ledger=None),
        api_token=os.environ["CELA_API_TOKEN"],
        store_url=os.environ["CELA_RUN_STORE_URL"],
        mcp_enabled_flag=False,
        reach_enabled=False,
        launcher_enabled=False,
    )
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
