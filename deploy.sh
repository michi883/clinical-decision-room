#!/usr/bin/env bash
set -euo pipefail

# --- Load .env if present ---
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

# --- Configuration ---
PROJECT_ID="${GCP_PROJECT_ID:-clinical-decision-room}"
REGION="${GCP_REGION:-us-central1}"
SERVICE_NAME="clinical-decision-room"
PORT=9999
MEMORY="512Mi"
TIMEOUT=60

# --- Validate prerequisites ---
if ! command -v gcloud &> /dev/null; then
  echo "ERROR: gcloud CLI not found. Install it from https://cloud.google.com/sdk/docs/install"
  exit 1
fi

if [ -z "${GEMINI_API_KEY:-}" ]; then
  echo "ERROR: GEMINI_API_KEY env var is not set."
  echo "  export GEMINI_API_KEY=your-api-key"
  exit 1
fi

echo "==> Deploying ${SERVICE_NAME} to Cloud Run"
echo "    Project: ${PROJECT_ID}"
echo "    Region:  ${REGION}"
echo ""

FIRST_DEPLOY=true

if [ "$FIRST_DEPLOY" = true ]; then
  # --- Enable required APIs ---
  echo "==> Enabling required GCP APIs..."
  gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    compute.googleapis.com \
    --project="${PROJECT_ID}" --quiet

  # --- Grant required IAM roles to Compute Engine default SA ---
  PROJECT_NUM=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")
  COMPUTE_SA="${PROJECT_NUM}-compute@developer.gserviceaccount.com"

  echo "==> Granting IAM roles to ${COMPUTE_SA}..."
  for ROLE in roles/artifactregistry.writer roles/storage.admin roles/run.admin roles/logging.logWriter; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${COMPUTE_SA}" \
      --role="${ROLE}" --quiet > /dev/null 2>&1
  done
fi

# --- Deploy ---
echo "==> Building and deploying (this takes 2-3 minutes on first deploy)..."
gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --set-env-vars "GEMINI_API_KEY=${GEMINI_API_KEY}" \
  --port "${PORT}" \
  --memory "${MEMORY}" \
  --timeout "${TIMEOUT}" \
  --project "${PROJECT_ID}" \
  --quiet

# --- Get service URL and update PUBLIC_URL ---
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --format="value(status.url)")

echo "==> Setting PUBLIC_URL to ${SERVICE_URL}..."
gcloud run services update "${SERVICE_NAME}" \
  --region "${REGION}" \
  --set-env-vars "GEMINI_API_KEY=${GEMINI_API_KEY},PUBLIC_URL=${SERVICE_URL}" \
  --project "${PROJECT_ID}" \
  --quiet > /dev/null 2>&1

echo ""
echo "=== Deployment complete ==="
echo ""
echo "Service URL:    ${SERVICE_URL}"
echo "Agent Card:     ${SERVICE_URL}/.well-known/agent-card.json"
echo ""
echo "To connect in Prompt Opinion:"
echo "  Agents → External Agents → Add Connection → paste the Agent Card URL above"
