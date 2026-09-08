# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Stage 2 of the certification trigger: codegen EMITS the handler (ITA I-8).

Stage 1 has a human supply `cert_trigger_url` for a hand-written stub. Because
this platform generates the External API's code, it can generate a conforming
handler beside it at a well-known versioned path under the same base URL — so
**deployment stays out of scope**: nothing new to deploy, only more of what
already is. The AUTHORITY SIDE IS IDENTICAL IN BOTH STAGES; a stub is replaced
by generated code without the platform noticing.

**Emitted deterministically from a template, never by the model.** The rest of
codegen is an LLM writing whole files, which is right for business logic and
wrong here: this handler's contract is exact — 202 and never a verdict, refuse
without the bearer secret, call the alias it was handed — and "the model
usually gets it right" is not a certification control. A template also makes
the emitted source testable by EXECUTING it (see the tests), which is the only
way to show a generated app satisfies the same contract a stub did.

Guardrails, all three from §3.5:
  * emitted ONLY under the codegen flag (`cert_emit_trigger_handler`);
  * refuses without the bearer secret;
  * maps `test_case_id` -> which outbound flow to originate, from the suite —
    an unknown case id is refused, not silently accepted, because accepting
    it would report "started" for a case nothing will ever run.
"""
from __future__ import annotations

import json
import logging
from typing import Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = ["TRIGGER_PATH", "emit_trigger_handler"]

TRIGGER_PATH = "/__cert/v1/trigger"

_TEMPLATE = '''"""Certification trigger handler — GENERATED, do not edit.

Emitted by the partner platform's codegen (ITA I-8, plan §3.5 Stage 2). It
asks THIS application to originate a certification case; it answers 202 and
never a verdict, because the outcome must arrive as this app's real outbound
call travelling through the tunnel. An application that could answer "passed"
here would be certifying the trigger instead of the implementation.

The bearer secret is read from the environment ({secret_env}) — never baked
into source. With it unset the handler refuses every call: an open trigger
lets anyone drive a certification run against this deployment.
"""
import logging
import os

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

logger = logging.getLogger(__name__)
router = APIRouter()

# test_case_id -> the outbound flow this app originates for it. Taken from the
# certification suite at generation time; an id absent here is refused rather
# than accepted, so a case nothing will run is never reported as started.
CASE_FLOWS = {case_flows}

SECRET_ENV = "{secret_env}"


def _authorise(authorization: str | None) -> None:
    expected = os.environ.get(SECRET_ENV)
    if not expected:
        raise HTTPException(status_code=503,
                            detail="certification trigger is not configured")
    if authorization != f"Bearer {{expected}}":
        raise HTTPException(status_code=401, detail="invalid trigger secret")


@router.post("{trigger_path}", status_code=202)
async def certification_trigger(request: Request,
                                background: BackgroundTasks,
                                authorization: str | None = Header(default=None)):
    _authorise(authorization)
    body = await request.json()
    test_case_id = (body or {{}}).get("test_case_id")
    if test_case_id not in CASE_FLOWS:
        raise HTTPException(status_code=404,
                            detail=f"no outbound flow for {{test_case_id!r}}")

    background.add_task(
        _originate_logged,
        test_case_id=test_case_id,
        flow=CASE_FLOWS[test_case_id],
        case_data=(body or {{}}).get("case_data") or {{}},
        cert_context=(body or {{}}).get("cert_context") or {{}},
        reply_via=(body or {{}}).get("reply_via"),
    )
    # 202 = "started", never a verdict.
    return {{"accepted": True, "test_case_id": test_case_id}}


async def _originate_logged(*, test_case_id: str, **kwargs) -> None:
    """Run the origination and LOG any failure.

    The 202 has already been sent by the time this runs, so an exception here
    would otherwise vanish — and a case that was accepted but never
    originated reports nothing, leaving the certification run to wait out its
    full suite deadline before recording it as unreported. A loud log is the
    difference between diagnosing that in seconds and in an hour.
    """
    try:
        await originate_case(**kwargs)
    except Exception:
        logger.exception(
            "certification trigger: case %s was ACCEPTED but origination "
            "failed — it will never report, and the run will wait out its "
            "deadline", test_case_id)


async def originate_case(*, flow: str, case_data: dict, cert_context: dict,
                         reply_via: str | None) -> None:
    """Originate one case: make this application's REAL outbound call for
    `flow`, addressed to `reply_via` (an ALIAS — this app's own tunnel
    ingress resolves it; no authority address is embedded here).

    IMPLEMENT THIS against the generated API's own client. It is left
    unimplemented on purpose: the outbound call is the thing under test, and
    a generated stand-in would certify the generator instead of the
    application.
    """
    raise NotImplementedError(
        "wire originate_case to the generated outbound client for flow "
        + flow)
'''


def emit_trigger_handler(
    *,
    case_flows: Mapping[str, str],
    enabled: bool,
    secret_env: str = "CERT_TRIGGER_SECRET",
    path: str = "app/cert_trigger.py",
) -> list[dict]:
    """The generated trigger handler as a codegen file entry, or [].

    Returns a LIST so a caller can `files += emit_trigger_handler(...)`
    unconditionally — the flag-off case is an empty list, not a None to
    special-case at every call site.
    """
    if not enabled:
        return []
    if not case_flows:
        logger.warning(
            "cert trigger codegen: flag on but the suite mapped no cases to "
            "outbound flows — emitting nothing rather than a handler that "
            "refuses every id")
        return []
    content = _TEMPLATE.format(
        case_flows=json.dumps(dict(sorted(case_flows.items())), indent=4),
        secret_env=secret_env,
        trigger_path=TRIGGER_PATH,
    )
    return [{"path": path, "content": content}]


def case_flow_map(cases: Sequence[Mapping]) -> dict[str, str]:
    """`test_case_id -> flow` from the certification suite's own rows.

    Only cases THIS side originates carry a flow: an authority-initiated case
    is not something this application starts, so mapping one would emit a
    trigger entry that must never fire.
    """
    out: dict[str, str] = {}
    for case in cases or ():
        case_id = case.get("case_id") or case.get("test_case_id")
        initiator = str(case.get("initiator") or "npci").strip().lower()
        flow = case.get("flow") or case.get("api")
        if case_id and flow and initiator not in ("npci", ""):
            out[str(case_id)] = str(flow)
    return out
