# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: the active domain pack's vocabulary, for the SPA.

WHY THIS ENDPOINT EXISTS. The pack is a server-side YAML file (`DOMAIN_PACK`),
but the roles and identifiers it defines are collected in the browser — the
Declare Ready dialog offers a role and asks for that role's identifiers. Until
now that dialog hardcoded four UPI roles and their VPA/IFSC/MCC fields, so a
non-payments deployment could not select a valid role at all and nothing
downstream could be exercised.

DELIBERATELY UNAUTHENTICATED-ADJACENT BUT STILL BEHIND THE SESSION. A pack holds
no secrets — it is vocabulary that appears in the UI and on a signed PDF — so
this could be public. It sits behind `get_current_user` anyway because every
other `/api` route does, and an endpoint that is the lone exception invites the
next person to copy the exception rather than the rule.

READ-ONLY BY DESIGN. There is no PUT. The pack is deployment configuration
chosen by whoever runs the platform, not something a dashboard user edits — and
`data/partner_profile.md` already exists for the things an operator DOES author.
"""
import logging

from fastapi import APIRouter, Depends

from app.api.auth import get_current_user
from app.core.domain import get_active_pack
from app.models import PartnerUser

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/domain")
def get_domain(user: PartnerUser = Depends(get_current_user)) -> dict:
    """Roles, their identifier fields, and the sign-off labels.

    Shaped to be consumed directly: the SPA builds its role picker from
    `roles` in order, and its per-role form from each role's `fields`. That is
    the same list the sign-off PDF labels its metadata rows from, so the dialog
    cannot drift from the document.

    `roles` is legitimately EMPTY for the generic pack — there is no neutral
    guess at what roles an unknown ecosystem has. The SPA must handle that by
    saying so, not by rendering an empty dropdown.
    """
    pack = get_active_pack()
    return {
        "pack": pack.name,
        "roles": [
            {
                "key": r.key,
                "label": r.label,
                "case_prefix": r.case_prefix,
                "fields": [
                    {
                        "key": f.key,
                        "label": f.label,
                        "placeholder": f.placeholder,
                        "required": f.required,
                    }
                    for f in r.fields
                ],
            }
            for r in pack.roles
        ],
        "signoff": {
            "issuer": pack.signoff.issuer,
            "programme": pack.signoff.programme,
        },
    }
