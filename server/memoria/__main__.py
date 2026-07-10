"""Entry point: `python -m memoria` starts the API server.

Bound to 127.0.0.1 on purpose — the backend must never be reachable from
the network, only from the frontend running on this same machine.
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("memoria.main:app", host="127.0.0.1", port=8123, log_level="info")
