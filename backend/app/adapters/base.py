"""The seam between the A2A core and a bank's own systems.

The core owns the protocol: message shapes, verdict triage, the txn relay. A
partner owns only what is specific to their deployment — who the bank is, where
its switch listens, and what test data to declare per case. Implement
`SwitchAdapter`, point `SWITCH_ADAPTER` at it, and the core never learns what
sits behind it (a real switch, a config file, a simulator).

Adapters return plain dicts and dataclasses; no ORM objects cross this boundary.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from fastapi import APIRouter


@dataclass(frozen=True)
class BankConfig:
    """What the bank tells NPCI in `cert_config_submission`."""

    # The body of the reply's `config` key — the spec's nested bank_identity /
    # network / security plus the flat extension keys precert's mapper prefers.
    # Must carry `requested_subset`.
    config: dict
    # The identity echoed back when NPCI asks about a specific sub-entry.
    psp_org_id: str
    # "host:port" of the bank's switch, for the human-readable summary.
    switch_endpoint: str


@dataclass(frozen=True)
class CaseData:
    """Per-case test data for `cert_test_preparation`."""

    # case_id -> values. NPCI merges these into the request it fires, so nothing
    # diagnostic may go in here — an extra key is sent as a payment field.
    case_data: dict[str, dict]
    # Appended to "N declared ready to execute" in the summary, e.g.
    # " (3 from the bank's store, 2 generated)". Include the leading separator.
    detail: str = ""
    # Extra top-level reply keys NPCI ignores — diagnostics that must stay OUT
    # of `case_data`.
    extras: dict = field(default_factory=dict)


class SwitchAdapter(ABC):
    """Everything about the bank that the A2A core cannot know."""

    @abstractmethod
    def bank_config(self, subset_hint: str) -> BankConfig:
        """The bank's certification config.

        `subset_hint` is NPCI's suggestion; the subset is the bank's choice, so
        use it only when the bank has not named one.
        """

    @abstractmethod
    def case_data(self, subset: str, case_ids: list[str], payload: dict) -> CaseData:
        """Test data for the announced cases. Must cover every id in `case_ids`.

        `payload` is the full `cert_setup_notification` payload, for adapters
        that need more than the ids (e.g. `case_list[].npci_data`).
        """

    @abstractmethod
    def switch_base(self, product: str, org_id: str) -> Optional[str]:
        """Base URL of the switch fronting `product/org_id`, e.g.
        `https://switch.bank.example:8443`; None if this bank fronts no such
        identity. Used to forward transaction messages — refuse on None, never
        guess."""

    @abstractmethod
    def bank_org_id(self) -> Optional[str]:
        """The bank's org id, used to pick the matching NPCI api key when the
        platform authenticates as this bank."""

    # Optional hooks. The defaults do nothing, so a partner adapter that only
    # answers the four questions above never has to know these exist. They are how
    # the internal simulator plugin layers test tooling on the core.

    async def on_reply(self, task_type: str, payload: dict, reply: dict, *,
                       change_id: Optional[str], cflow_id: Optional[str]) -> dict:
        """Last look at the reply for an inbound task before it goes to NPCI.

        Return the body to send. May edit it, or wait (the reply is the response
        NPCI is blocked on, whose client gives up at 30s)."""
        return reply

    async def after_reply(self, task_type: str, payload: dict, reply: dict, *,
                          change_id: Optional[str], cflow_id: Optional[str]) -> None:
        """Called after the reply has been completed back to NPCI, so slow work
        (e.g. raising bank-initiated cases) cannot hold NPCI up. Exceptions are
        logged by the caller, never propagated."""
        return None

    def api_router(self) -> "Optional[APIRouter]":
        """Extra HTTP routes mounted on the partner API. None for a plain adapter.

        Whatever is mounted here is as exposed as the partner API itself, so only
        internal test tooling should use this, and it should say so."""
        return None
