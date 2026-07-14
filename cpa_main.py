from __future__ import annotations

import uvicorn

from cpa_inspector.constants import DEFAULT_HOST, DEFAULT_PORT
from cpa_inspector.web.app import create_app


def run() -> None:
    uvicorn.run(create_app(), host=DEFAULT_HOST, port=DEFAULT_PORT, reload=False)


if __name__ == "__main__":
    run()
