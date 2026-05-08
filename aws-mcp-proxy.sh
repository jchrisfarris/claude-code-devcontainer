#!/bin/sh
# AWS Agent Toolkit MCP proxy launcher.
#
# Resolves AWS_REGION from the default credential chain at launch time so the
# region is sourced from whatever profile / env the container is currently
# using, instead of being hardcoded into the plugin's bundled .mcp.json.
#
# Resolution order:
#   1. $AWS_REGION
#   2. $AWS_DEFAULT_REGION
#   3. `aws configure get region` (honors $AWS_PROFILE)
#   4. us-east-1 (last-resort fallback)
set -eu

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
if [ -z "${REGION}" ]; then
  REGION="$(aws configure get region 2>/dev/null || true)"
fi
REGION="${REGION:-us-east-1}"

exec uvx mcp-proxy-for-aws@latest \
  https://aws-mcp.us-east-1.api.aws/mcp \
  --metadata "AWS_REGION=${REGION}"
