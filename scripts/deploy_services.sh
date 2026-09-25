#!/usr/bin/env bash
# ==============================================================================
# Cloud Run Deployment Script called by Cloud Build
# ==============================================================================
set -euo pipefail

echo "=============================================================================="
echo "==> Deploying MCP Server and Agent Services to Cloud Run"
echo "Project: $PROJECT_ID | Region: $REGION | Image Tag: $COMMIT_SHA"
echo "=============================================================================="

# 1. Check for Slurm secret in Secret Manager
SECRET_FLAG=""
if [ -n "${SLURM_SECRET_NAME:-}" ] && [ "$SLURM_SECRET_NAME" != "none" ]; then
  if gcloud secrets describe "$SLURM_SECRET_NAME" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "Binding secret $SLURM_SECRET_NAME to SLURM_JWT_TOKEN"
    SECRET_FLAG="--set-secrets=SLURM_JWT_TOKEN=${SLURM_SECRET_NAME}:latest"
  else
    echo "Notice: Secret $SLURM_SECRET_NAME not viewable or not found via describe. Attempting direct secret binding."
    SECRET_FLAG="--set-secrets=SLURM_JWT_TOKEN=${SLURM_SECRET_NAME}:latest"
  fi
fi

# 2. Check for Direct VPC Egress
VPC_FLAGS=""
if [ -n "${VPC_NETWORK:-}" ] && [ "$VPC_NETWORK" != "none" ]; then
  echo "Enabling Direct VPC Egress on network: $VPC_NETWORK, subnet: $VPC_SUBNET"
  VPC_FLAGS="--network=$VPC_NETWORK --subnet=$VPC_SUBNET --vpc-egress=private-ranges-only"
fi

# 3. Deploy MCP Server
MCP_MIN_INSTANCES="${MCP_MIN_INSTANCES:-0}"
echo "==> Deploying $MCP_SERVICE_NAME (min-instances: $MCP_MIN_INSTANCES)..."
gcloud run deploy "$MCP_SERVICE_NAME" \
  --project="$PROJECT_ID" \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/mcp-server:${COMMIT_SHA}" \
  --region="$REGION" \
  --platform=managed \
  --no-allow-unauthenticated \
  --no-cpu-throttling \
  --timeout=1800 \
  --session-affinity \
  --min-instances="$MCP_MIN_INSTANCES" \
  --set-env-vars="COMPUTE_RUNTIME=slurm,SLURM_REST_URL=${SLURM_REST_URL},MCP_TRANSPORT=sse" \
  $VPC_FLAGS \
  $SECRET_FLAG

# 4. Discover MCP Server URL
MCP_URL=$(gcloud run services describe "$MCP_SERVICE_NAME" --project="$PROJECT_ID" --region="$REGION" --format='value(status.url)')
echo "Discovered MCP Server URL: $MCP_URL"

# 5. Deploy Agent Service
AGENT_AUTH_FLAG="--allow-unauthenticated"
if [ "${REQUIRE_IAM_AUTH:-false}" = "true" ] || [ "${ALLOW_UNAUTHENTICATED:-true}" = "false" ]; then
  AGENT_AUTH_FLAG="--no-allow-unauthenticated"
fi

AGENT_ENV_VARS="MCP_SERVER_URL=${MCP_URL},GOOGLE_GENAI_USE_VERTEXAI=TRUE,AGENTIC_COMPUTE_MODEL=${MODEL},GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION}"
AGENT_SECRETS=""
AGENT_VPC_FLAGS=""

# The dashboard's "Run" submits through the agent service's own runtime
# (compute_agent/app.py), never through the MCP server. Without the Slurm
# settings the agent falls back to its in-process simulator while its
# /api/snapshot shows the MCP server's real cluster, so runs never reached the
# cluster on screen. Set AGENT_COMPUTE_RUNTIME=simulator to keep a demo agent.
AGENT_COMPUTE_RUNTIME="${AGENT_COMPUTE_RUNTIME:-slurm}"
if [ "$AGENT_COMPUTE_RUNTIME" = "slurm" ]; then
  echo "Connecting $AGENT_SERVICE_NAME to Slurm at $SLURM_REST_URL"
  AGENT_ENV_VARS="${AGENT_ENV_VARS},COMPUTE_RUNTIME=slurm,SLURM_REST_URL=${SLURM_REST_URL}"
  AGENT_VPC_FLAGS="$VPC_FLAGS"
  if [ -n "$SECRET_FLAG" ]; then
    AGENT_SECRETS="SLURM_JWT_TOKEN=${SLURM_SECRET_NAME}:latest"
  fi
else
  echo "Notice: $AGENT_SERVICE_NAME runs the simulator (AGENT_COMPUTE_RUNTIME=$AGENT_COMPUTE_RUNTIME); dashboard runs will not reach Slurm."
fi

if [ -n "${AGENTGRID_API_KEY_SECRET:-}" ] && [ "$AGENTGRID_API_KEY_SECRET" != "none" ]; then
  AGENT_SECRETS="${AGENT_SECRETS:+${AGENT_SECRETS},}AGENTGRID_API_KEY=${AGENTGRID_API_KEY_SECRET}:latest"
fi

# One --set-secrets flag: a second one would replace the first.
AGENT_SECRET_FLAG=""
if [ -n "$AGENT_SECRETS" ]; then
  AGENT_SECRET_FLAG="--set-secrets=${AGENT_SECRETS}"
fi

echo "==> Deploying $AGENT_SERVICE_NAME (Auth: $AGENT_AUTH_FLAG, runtime: $AGENT_COMPUTE_RUNTIME)..."
gcloud run deploy "$AGENT_SERVICE_NAME" \
  --project="$PROJECT_ID" \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/agent-service:${COMMIT_SHA}" \
  --region="$REGION" \
  --platform=managed \
  $AGENT_AUTH_FLAG \
  --no-cpu-throttling \
  --timeout=1800 \
  --session-affinity \
  --set-env-vars="$AGENT_ENV_VARS" \
  $AGENT_VPC_FLAGS \
  $AGENT_SECRET_FLAG

# 6. Authorize Agent Service to invoke private MCP Server via IAM OIDC
AGENT_SA=$(gcloud run services describe "$AGENT_SERVICE_NAME" --project="$PROJECT_ID" --region="$REGION" --format='value(spec.template.spec.serviceAccountName)' || true)
if [ -z "$AGENT_SA" ]; then
  if [ -n "${PROJECT_NUMBER:-}" ]; then
    AGENT_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
  else
    PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
    AGENT_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
  fi
fi

echo "Granting roles/run.invoker on $MCP_SERVICE_NAME to $AGENT_SA..."
gcloud run services add-iam-policy-binding "$MCP_SERVICE_NAME" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --member="serviceAccount:${AGENT_SA}" \
  --role="roles/run.invoker"

echo "=============================================================================="
echo "==> Successfully deployed both services to Cloud Run!"
echo "Agent Service: $(gcloud run services describe "$AGENT_SERVICE_NAME" --project="$PROJECT_ID" --region="$REGION" --format='value(status.url)')"
echo "MCP Service:   $MCP_URL (private IAM)"
echo "=============================================================================="
