#!/bin/sh
#  Copyright (c) 2026 Cisco Systems, Inc. and its affiliates
#  SPDX-License-Identifier: Apache-2.0
set -eu

exec /opt/venv/bin/semantic-analysis-worker-cli "$@"
