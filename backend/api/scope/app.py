#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
"""Entry point for the SCOPE API service.

Run with:
    uvicorn scope.app:app --reload
"""

import logging
import os

import uvicorn
from dotenv import load_dotenv

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s:%(funcName)s] %(message)s",
    level=logging.INFO,
)

# Load environment variables from .env file
load_dotenv()

port = int(os.getenv("SCOPE_API_PORT", 8000))
reload = os.getenv("SCOPE_API_RELOAD", "true").lower() == "true"
print(f"Starting SCOPE API on port {port}...")


def main():
    """Run the SCOPE API server."""
    uvicorn.run("scope.api:app", host="0.0.0.0", port=port, reload=reload)
