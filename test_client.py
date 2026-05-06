"""Local test script for the Clinical Decision Room A2A agent."""

import httpx
import json
import sys

BASE_URL = "http://localhost:9999"


def test_agent_card():
    """Test the agent card endpoint."""
    print("=== Testing Agent Card ===")
    resp = httpx.get(f"{BASE_URL}/.well-known/agent-card.json")
    print(f"Status: {resp.status_code}")
    card = resp.json()
    print(f"Name: {card['name']}")
    print(f"Skills: {[s['name'] for s in card['skills']]}")
    print(f"Capabilities: {card['capabilities']}")
    print()
    return resp.status_code == 200


def test_message_send(text: str = "What is the differential diagnosis for a 45-year-old male with acute onset substernal chest pain, diaphoresis, and nausea?"):
    """Test sending a clinical query."""
    print("=== Testing message/send ===")
    print(f"Query: {text[:100]}...")
    payload = {
        "jsonrpc": "2.0",
        "id": "test-1",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
                "messageId": "msg-test-1",
            }
        },
    }
    resp = httpx.post(
        f"{BASE_URL}/",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=60.0,
    )
    print(f"Status: {resp.status_code}")
    result = resp.json()
    print(json.dumps(result, indent=2)[:2000])
    print()
    return resp.status_code == 200


def test_legacy_tasks_send(text: str = "What is the differential diagnosis for a 45-year-old male with acute onset substernal chest pain, diaphoresis, and nausea?"):
    """Test the legacy A2A v0.1 tasks/send method alias."""
    print("=== Testing legacy tasks/send ===")
    print(f"Query: {text[:100]}...")
    payload = {
        "jsonrpc": "2.0",
        "id": "legacy-test-1",
        "method": "tasks/send",
        "params": {
            "id": "legacy-task-test-1",
            "sessionId": "legacy-session-test-1",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": text}],
            },
            "historyLength": 1,
        },
    }
    resp = httpx.post(
        f"{BASE_URL}/",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=60.0,
    )
    print(f"Status: {resp.status_code}")
    result = resp.json()
    print(json.dumps(result, indent=2)[:2000])
    print()
    return resp.status_code == 200


def test_message_with_fhir():
    """Test sending a message with mock FHIR context."""
    print("=== Testing message/send with FHIR context ===")
    payload = {
        "jsonrpc": "2.0",
        "id": "test-2",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [
                    {
                        "kind": "text",
                        "text": "Review this patient's data and provide a clinical assessment.",
                    }
                ],
                "messageId": "msg-test-2",
                "metadata": {
                    "https://app.promptopinion.ai/schemas/a2a/v1/fhir-context": {
                        "fhirUrl": "https://launch.smarthealthit.org/v/r4/fhir",
                        "fhirToken": "demo-token",
                        "patientId": "smart-1288992",
                    }
                },
            }
        },
    }
    resp = httpx.post(
        f"{BASE_URL}/",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=60.0,
    )
    print(f"Status: {resp.status_code}")
    result = resp.json()
    print(json.dumps(result, indent=2)[:2000])
    print()
    return resp.status_code == 200


if __name__ == "__main__":
    tests = [test_agent_card, test_message_send]

    if "--legacy" in sys.argv:
        tests.append(test_legacy_tasks_send)

    if "--fhir" in sys.argv:
        tests.append(test_message_with_fhir)

    if len(sys.argv) > 1 and sys.argv[1] not in ("--fhir", "--legacy"):
        # Custom query
        test_agent_card()
        test_message_send(sys.argv[1])
    else:
        passed = sum(1 for t in tests if t())
        print(f"Results: {passed}/{len(tests)} tests passed")
