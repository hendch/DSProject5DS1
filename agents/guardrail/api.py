"""Layer 4: the human side of the guardrail, as a REST API.

The dashboard (Member F) calls these endpoints; the Orchestrator listens for
the approval_result events they publish.

Run:  uvicorn agents.guardrail.api:app --reload --port 8080
Docs: http://localhost:8080/docs
"""
import os
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agents.guardrail import store
from agents.guardrail.announce import announce

app = FastAPI(title="Guardrail approvals", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_methods=["*"], allow_headers=["*"],
                   allow_origins=os.getenv("GUARDRAIL_CORS_ORIGINS", "*").split(","))


class Decision(BaseModel):
    # Not authenticated yet: whoever calls says who they are. Fine for the MVP,
    # must become a real login before anything is applied automatically.
    decided_by: str = Field(min_length=1, max_length=100, examples=["ines"])


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/pending-approvals")
def pending_approvals():
    return store.list_pending()


@app.get("/approvals/{approval_id}")
def get_approval(approval_id: uuid.UUID):
    approval = store.get_approval(str(approval_id))
    if approval is None:
        raise HTTPException(404, "approval not found")
    return approval


def _vote(approval_id: uuid.UUID, body: Decision, vote: str) -> dict:
    try:
        result = store.cast_vote(str(approval_id), body.decided_by, vote)
    except store.ApprovalError as e:
        raise HTTPException(e.http_status, str(e)) from None
    if result["status"] != "pending":  # final: tell the Orchestrator
        announce(result["correlation_id"], result["environment"], result["risk"],
                 result["action_ref"], result["status"], result["decided_by"],
                 approval_id=result["approval_id"], approvals=result["approvals"])
    return result


@app.post("/approve/{approval_id}")
def approve(approval_id: uuid.UUID, body: Decision):
    return _vote(approval_id, body, "approve")


@app.post("/reject/{approval_id}")
def reject(approval_id: uuid.UUID, body: Decision):
    return _vote(approval_id, body, "reject")


@app.get("/audit/verify")
def verify_audit_log():
    broken = store.verify_chain()
    return {"intact": not broken, "broken_rows": broken}
