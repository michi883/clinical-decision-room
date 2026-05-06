import asyncio
import json
import os

import httpx
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from a2a.utils import new_text_artifact
from google import genai
from typing_extensions import override

FHIR_EXTENSION_URI = "https://app.promptopinion.ai/schemas/a2a/v1/fhir-context"

PANELIST_MODEL = "gemini-3.1-flash-lite-preview"
SYNTHESIZER_MODEL = "gemini-3-flash-preview"

_PANELIST_JSON_INSTRUCTION = """

You MUST respond with valid JSON only - no markdown, no code fences, no commentary outside the JSON object.
Use this exact schema:
{
  "framework": "<your_framework_id>",
  "top_assessments": [
    {"diagnosis": "...", "rationale": "..."}
  ],
  "recommended_actions": ["..."],
  "key_reasoning": "...",
  "confidence": "low|moderate|high"
}
"""

PANELIST_BAYESIAN = """You are a clinical reasoning engine that thinks ONLY in Bayesian probabilistic terms. You do not use pattern matching, guidelines, or worst-case thinking. You reason exclusively from base rates, pre-test probabilities, and likelihood ratios.

For each potential diagnosis:
1. State the prior (base rate) for the relevant population.
2. Identify the key findings and estimate how much each finding shifts the probability (likelihood ratio).
3. Arrive at a rough posterior probability.

Produce a ranked differential with approximate probability estimates. Show your priors and the evidence that moves them. Be numerically explicit - vague statements like "more likely" are not acceptable; give rough percentages or odds.
""" + _PANELIST_JSON_INSTRUCTION.replace("<your_framework_id>", "bayesian")

PANELIST_GESTALT = """You are a clinical reasoning engine that thinks ONLY in terms of illness scripts and pattern recognition (clinical gestalt). You do not use probabilistic reasoning, guidelines, or worst-case thinking. You reason exclusively by matching the presentation to known clinical patterns.

For each potential diagnosis:
1. Name the specific illness script you are matching against.
2. List which features of the presentation FIT the script.
3. List which features DO NOT FIT or are atypical.
4. Rate the overall script match (strong, moderate, weak).

Focus on the "snap diagnosis" - what does this *look like* to an experienced clinician? Name the classic presentations you are comparing to and be explicit about fit vs. misfit.
""" + _PANELIST_JSON_INSTRUCTION.replace("<your_framework_id>", "gestalt")

PANELIST_CANTMISS = """You are a clinical reasoning engine that thinks ONLY in adversarial worst-case terms. You do not use probabilistic reasoning, pattern matching, or guidelines. Your single job is to identify diagnoses that would cause serious harm if missed, even if they are unlikely.

For each can't-miss diagnosis:
1. Name the dangerous condition.
2. Explain why missing it would be catastrophic (morbidity/mortality).
3. List the specific findings in THIS presentation that are compatible with it.
4. List the specific tests or findings that would RULE IT IN or RULE IT OUT.

You are EXPECTED to raise alarm about things the other reasoning approaches might dismiss as improbable. That is your purpose. Never downplay a dangerous possibility just because it is rare. Err on the side of caution and dissent.
""" + _PANELIST_JSON_INSTRUCTION.replace("<your_framework_id>", "cantmiss")

PANELIST_GUIDELINES = """You are a clinical reasoning engine that thinks ONLY in terms of published clinical practice guidelines. You do not use probabilistic reasoning, pattern matching, or worst-case thinking. You reason exclusively from authoritative guideline recommendations.

For each relevant guideline:
1. Name the specific guideline (e.g., "2023 AHA/ACC Chest Pain Guideline").
2. State the recommendation class and level of evidence where possible (e.g., Class I, Level B-R).
3. Describe what the guideline says to do for a patient with this presentation.
4. Note any guideline-specific risk scores or decision tools that apply (e.g., HEART score, Wells criteria, CHA2DS2-VASc).

Cite real guidelines. Do not invent recommendations. If no guideline directly addresses the scenario, say so explicitly.
""" + _PANELIST_JSON_INSTRUCTION.replace("<your_framework_id>", "guidelines")

SYNTHESIZER_PROMPT = """You are a clinical synthesis engine. You have received analyses of the same patient case from four structurally distinct reasoning frameworks: Bayesian (probabilistic), Gestalt (pattern-based), Can't-Miss (worst-case adversarial), and Guidelines (evidence-based guidelines).

Your job is to synthesize their outputs. You MUST follow these rules:

1. **Preserve disagreement.** If panelists diverge, state plainly which panelist holds which view and why. NEVER average or smooth disagreements into a single lukewarm answer.
2. Identify points of **agreement** - diagnoses or actions all four (or most) converge on.
3. Identify points of **divergence** - where they split, and explain HOW the structural differences in their reasoning frameworks produce different answers.
4. State a **current recommendation** - the single best next action given the panel's collective view, honestly acknowledging uncertainty.
5. List **decision-flip conditions** - specific findings, lab results, imaging results, or clinical events that would change the recommendation. These are the things the clinician should actively watch for.

This is decision SUPPORT - not a replacement for clinical judgment.

You MUST respond with valid JSON only - no markdown, no code fences. Use this exact schema:
{
  "agreements": ["..."],
  "divergences": [
    {
      "topic": "...",
      "positions": {
        "bayesian": "...",
        "gestalt": "...",
        "cantmiss": "...",
        "guidelines": "..."
      }
    }
  ],
  "current_recommendation": "...",
  "decision_flip_conditions": ["..."]
}
"""


class ClinicalDecisionExecutor(AgentExecutor):
    def __init__(self) -> None:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        self.client = genai.Client(api_key=api_key)

    async def _fetch_fhir_data(self, fhir_ctx: dict) -> dict | None:
        """Fetch selected patient context from a FHIR server."""
        fhir_url = fhir_ctx.get("fhirUrl")
        fhir_token = fhir_ctx.get("fhirToken")
        patient_id = fhir_ctx.get("patientId")

        if not all([fhir_url, fhir_token, patient_id]):
            return None

        headers = {"Authorization": f"Bearer {fhir_token}"}
        patient_data = {}

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{fhir_url}/Patient/{patient_id}", headers=headers
            )
            if resp.status_code == 200:
                patient_data["patient"] = resp.json()

            resp = await client.get(
                f"{fhir_url}/Condition",
                params={"patient": patient_id, "_count": "20", "_sort": "-date"},
                headers=headers,
            )
            if resp.status_code == 200:
                patient_data["conditions"] = resp.json()

            resp = await client.get(
                f"{fhir_url}/MedicationRequest",
                params={"patient": patient_id, "status": "active", "_count": "20"},
                headers=headers,
            )
            if resp.status_code == 200:
                patient_data["medications"] = resp.json()

            resp = await client.get(
                f"{fhir_url}/Observation",
                params={
                    "patient": patient_id,
                    "category": "laboratory",
                    "_count": "20",
                    "_sort": "-date",
                },
                headers=headers,
            )
            if resp.status_code == 200:
                patient_data["lab_results"] = resp.json()

            resp = await client.get(
                f"{fhir_url}/AllergyIntolerance",
                params={"patient": patient_id},
                headers=headers,
            )
            if resp.status_code == 200:
                patient_data["allergies"] = resp.json()

        return patient_data if patient_data else None

    async def _call_panelist(
        self, system_prompt: str, case_content: str, framework: str
    ) -> dict:
        """Call one reasoning framework and return parsed JSON or an error object."""
        try:
            response = await self.client.aio.models.generate_content(
                model=PANELIST_MODEL,
                contents=case_content,
                config=genai.types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    max_output_tokens=4096,
                ),
            )
            return json.loads(response.text)
        except json.JSONDecodeError:
            return {
                "framework": framework,
                "error": "parse_failed",
                "raw": response.text,
            }
        except Exception as e:
            return {
                "framework": framework,
                "error": "call_failed",
                "raw": str(e),
            }

    async def _call_synthesizer(self, panelist_outputs: dict) -> dict:
        """Synthesize panelist outputs into one clinical decision support response."""
        content = (
            "Here are the four panelist analyses for the same clinical case:\n\n"
            + json.dumps(panelist_outputs, indent=2)
        )
        try:
            response = await self.client.aio.models.generate_content(
                model=SYNTHESIZER_MODEL,
                contents=content,
                config=genai.types.GenerateContentConfig(
                    system_instruction=SYNTHESIZER_PROMPT,
                    response_mime_type="application/json",
                    max_output_tokens=4096,
                ),
            )
            return json.loads(response.text)
        except json.JSONDecodeError:
            return {"error": "parse_failed", "raw": response.text}
        except Exception as e:
            return {"error": "call_failed", "raw": str(e)}

    @override
    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        user_input = context.get_user_input()

        fhir_ctx = None
        patient_context = ""
        if context.message.metadata:
            fhir_ctx = context.message.metadata.get(FHIR_EXTENSION_URI)

        if fhir_ctx:
            try:
                patient_data = await self._fetch_fhir_data(fhir_ctx)
                if patient_data:
                    patient_context = (
                        "\n\n--- PATIENT DATA (from FHIR) ---\n"
                        + json.dumps(patient_data, indent=2, default=str)
                        + "\n--- END PATIENT DATA ---\n"
                    )
            except Exception as e:
                patient_context = (
                    f"\n\n[Note: Could not fetch FHIR patient data: {e}]\n"
                )

        case_content = user_input + patient_context

        bayesian, gestalt, cantmiss, guidelines = await asyncio.gather(
            self._call_panelist(PANELIST_BAYESIAN, case_content, "bayesian"),
            self._call_panelist(PANELIST_GESTALT, case_content, "gestalt"),
            self._call_panelist(PANELIST_CANTMISS, case_content, "cantmiss"),
            self._call_panelist(PANELIST_GUIDELINES, case_content, "guidelines"),
        )

        panelist_outputs = {
            "bayesian": bayesian,
            "gestalt": gestalt,
            "cantmiss": cantmiss,
            "guidelines": guidelines,
        }

        synthesis = await self._call_synthesizer(panelist_outputs)
        final_result = {
            "panelists": panelist_outputs,
            "synthesis": synthesis,
        }

        artifact = new_text_artifact(
            name="clinical-panel-analysis",
            text=json.dumps(final_result, indent=2),
            description="Clinical panel analysis with four reasoning frameworks and synthesis",
        )
        await event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                taskId=context.task_id,
                contextId=context.context_id,
                artifact=artifact,
            )
        )

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                taskId=context.task_id,
                contextId=context.context_id,
                status=TaskStatus(state=TaskState.completed),
                final=True,
            )
        )

    @override
    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        raise Exception("cancel not supported")
