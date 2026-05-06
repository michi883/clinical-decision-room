# Clinical Decision Room

Clinical Decision Room is an AI-powered clinical decision support agent that analyzes patient cases through four independent reasoning frameworks and synthesizes their outputs into a structured report. It is built as an [A2A (Agent-to-Agent)](https://github.com/a2aproject/a2a-python) server and designed to be consumed by external agent hosts such as [Prompt Opinion](https://app.promptopinion.ai).

> **Not a medical device.** This project is experimental decision support only. It does not diagnose patients and is not a replacement for clinician judgment.

## How It Works

Given a clinical scenario (and optionally FHIR patient context), the agent runs four Gemini-powered reasoning "panelists" in parallel, each analyzing the same case from a structurally different perspective:

| Framework | Approach |
| --- | --- |
| **Bayesian** | Starts from base rates, applies likelihood ratios, produces a ranked differential with approximate probability estimates |
| **Gestalt** | Matches the presentation against known illness scripts, rates script fit, identifies atypical features |
| **Can't-Miss** | Adversarial worst-case reasoning — flags dangerous diagnoses that would cause serious harm if missed, even when unlikely |
| **Guidelines** | Cites published clinical practice guidelines, recommendation classes, levels of evidence, and applicable risk scores |

A fifth Gemini call synthesizes the four outputs into a final report that preserves:

- **Agreements** — diagnoses or actions most panelists converge on
- **Divergences** — where frameworks disagree and why their structural differences produce different answers
- **Current recommendation** — single best next action, with honest uncertainty
- **Decision-flip conditions** — specific findings that would change the recommendation

The final output is returned as a structured JSON artifact named `clinical-panel-analysis`.

## Architecture

```
                        ┌─────────────────────────────┐
                        │     A2A Client (e.g.        │
                        │     Prompt Opinion BYO)     │
                        └────────────┬────────────────┘
                                     │ JSON-RPC
                        ┌────────────▼────────────────┐
                        │   Compatibility Middleware   │
                        │  (method & schema aliasing)  │
                        ├─────────────────────────────┤
                        │   A2A Server (Starlette)    │
                        │   a2a-sdk 0.3.25            │
                        ├─────────────────────────────┤
                        │   ClinicalDecisionExecutor  │
                        ├──────┬──────┬──────┬────────┤
                        │Bayes │Gest. │Can't │Guide-  │
                        │ ian  │ alt  │Miss  │lines   │  ← 4 parallel Gemini calls
                        ├──────┴──────┴──────┴────────┤
                        │       Synthesizer           │  ← 1 Gemini call
                        └─────────────────────────────┘
```

### A2A Compatibility Layer

The A2A protocol has evolved through several versions and dialect variations. The middleware in `__main__.py` normalizes both requests and responses so the agent works with a range of clients:

**Request normalization** — rewrites incoming JSON-RPC payloads to the format expected by `a2a-sdk 0.3.25`:
- Method aliases: `tasks/send`, `SendMessage`, `Request`, `message:send` → `message/send`
- Message fields: `content` → `parts`, `type` → `kind` (on parts), `sessionId` → `contextId`
- Role aliases: `ROLE_USER` → `user`, `ROLE_AGENT` → `agent`

**Response normalization** — for proto-style clients (those using RPC-style method names like `SendMessage`), converts enum values back to their proto representation:
- Role aliases: `user` → `ROLE_USER`, `agent` → `ROLE_AGENT`
- Task state aliases: `completed` → `TASK_STATE_COMPLETED`, etc.

**Agent card** — served at both `/.well-known/agent.json` and `/.well-known/agent-card.json`, with a superset payload that includes A2A v0.3 fields and v1-style `supportedInterfaces`.

### FHIR Integration

When the incoming message includes FHIR context metadata (under the extension URI `https://app.promptopinion.ai/schemas/a2a/v1/fhir-context`), the executor fetches patient data from the specified FHIR server before running the panelists. It retrieves:

- Patient demographics
- Conditions (up to 20, most recent first)
- Active medications (up to 20)
- Lab results (up to 20, most recent first)
- Allergy intolerances

This data is appended to the clinical question so the panelists can incorporate it into their analysis.

## Requirements

- Python 3.11+
- A [Google Gemini API key](https://aistudio.google.com/apikey)
- Optional: [ngrok](https://ngrok.com/) for exposing a local server to external clients
- Optional: [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) for Cloud Run deployment

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Edit `.env` with your Gemini API key:

```bash
GEMINI_API_KEY=your-api-key-here
PORT=9999
PUBLIC_URL=http://localhost:9999
NGROK_URL=
```

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `GEMINI_API_KEY` | Yes | — | API key for Gemini (used by `google-genai`) |
| `PORT` | No | `9999` | HTTP server port |
| `PUBLIC_URL` | No | `http://localhost:9999` | URL published in the A2A agent card |
| `NGROK_URL` | No | — | Reserved ngrok domain (e.g. `your-domain.ngrok-free.dev`) |

## Running Locally

Start the server:

```bash
python -m clinical_decision_room
```

Verify the agent card:

```bash
curl http://localhost:9999/.well-known/agent.json
```

Send a sample clinical query:

```bash
curl -X POST http://localhost:9999/ \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "message/send",
    "params": {
      "message": {
        "role": "user",
        "parts": [
          {
            "kind": "text",
            "text": "45-year-old male with acute substernal chest pain, diaphoresis, nausea, hypertension, and smoking history. Differential and workup?"
          }
        ],
        "messageId": "m1"
      }
    }
  }'
```

## Connecting to Prompt Opinion

To connect a local server to [Prompt Opinion](https://app.promptopinion.ai), expose it through ngrok.

Set your ngrok domain in `.env`:

```bash
NGROK_URL=your-domain.ngrok-free.dev
PUBLIC_URL=https://your-domain.ngrok-free.dev
```

Then run both the server and tunnel together:

```bash
./run_local.sh
```

In Prompt Opinion:

1. Go to **Agents > External Agents > Add Connection**.
2. Paste `https://YOUR-DOMAIN.ngrok-free.dev/.well-known/agent.json`.
3. Open **Launchpad** and start a chat with a BYO agent.
4. Select **Clinical Decision Room** from the external-agent dropdown.
5. Ask a clinical question.

## Test Client

A smoke-test script is included for quick verification. With the server running:

```bash
# Default clinical query
python test_client.py

# Custom query
python test_client.py "What is the differential for sudden severe headache with neck stiffness?"

# Include FHIR context test (uses SMART Health IT sandbox)
python test_client.py --fhir

# Include legacy tasks/send compatibility test
python test_client.py --legacy
```

## Docker

```bash
docker build -t clinical-decision-room .
docker run \
  -e GEMINI_API_KEY=your-key \
  -e PUBLIC_URL=http://localhost:9999 \
  -p 9999:9999 \
  clinical-decision-room
```

## Deploy to Google Cloud Run

Prerequisites: `gcloud` CLI installed and authenticated, a Google Cloud project, and `GEMINI_API_KEY` set.

```bash
./deploy.sh
```

Override project or region:

```bash
GCP_PROJECT_ID=my-project GCP_REGION=europe-west1 ./deploy.sh
```

| Setting | Default |
| --- | --- |
| Project | `clinical-decision-room` |
| Region | `us-central1` |
| Port | `9999` |
| Memory | `512Mi` |
| Timeout | `60s` |

The script enables required GCP APIs, builds from source, deploys to Cloud Run with unauthenticated access, and sets `PUBLIC_URL` to the deployed service URL.

## Project Structure

```
clinical-decision-room/
├── clinical_decision_room/
│   ├── __init__.py
│   ├── __main__.py          # A2A server, agent card, compatibility middleware
│   └── agent_executor.py    # Gemini panelists, FHIR fetching, synthesis
├── test_client.py           # Smoke-test client
├── run_local.sh             # Local server + ngrok tunnel
├── deploy.sh                # Cloud Run deployment
├── Dockerfile
├── pyproject.toml
├── .env.example
├── .dockerignore
└── .gitignore
```

## Gemini Models

The panelists and synthesizer use configurable models defined at the top of `agent_executor.py`:

| Role | Model |
| --- | --- |
| Panelists (x4) | `gemini-3.1-flash-lite-preview` |
| Synthesizer | `gemini-3-flash-preview` |

Review and update these before production use — preview models may be deprecated or rotated.

## Important Notes

- **Do not commit `.env` or API keys.** The `.gitignore` excludes `.env` by default.
- **FHIR data is sensitive health information.** Deploy only in environments that meet your privacy, security, and compliance requirements.
- **Add authentication** or network restrictions before exposing the service beyond trusted integration paths.
- **Add automated tests** before depending on this in a critical workflow. The included test client is a smoke test, not a test suite.
