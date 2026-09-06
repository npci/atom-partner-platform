# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""`read_envelope` must be TYPE-checked but still FIELD-tolerant.

The security contract asks for `validation_failure_behavior: "reject"`. Full
strict rejection would break a legacy NPCI that omits newer envelope fields, so
the closure here is narrower and deliberate: reject wrong-TYPED values (the
exploitable half — type confusion from an H3 boundary), keep tolerating MISSING
ones (the compatibility promise).
"""
from app.a2a_common.protocol import read_envelope


class TestTypeConfusionBlocked:
    def test_non_dict_payload_reads_as_empty(self):
        """Handlers are annotated `payload: dict` and index into it. A string
        payload previously flowed straight through to them."""
        env = read_envelope({"task_type": "query", "payload": "i-am-a-string"})
        assert env.payload == {}
        assert isinstance(env.payload, dict)

    def test_list_payload_reads_as_empty(self):
        assert read_envelope({"payload": [1, 2, 3]}).payload == {}

    def test_non_string_task_type_reads_as_empty(self):
        """Empty task_type trips the executor's existing `if not task_type`
        guard, so the message is rejected at the boundary."""
        assert read_envelope({"task_type": ["a", "b"]}).task_type == ""
        assert read_envelope({"task_type": {"k": "v"}}).task_type == ""
        assert read_envelope({"task_type": 42}).task_type == ""

    def test_dict_change_id_reads_as_none(self):
        """A dict change_id would otherwise reach a DB lookup."""
        assert read_envelope({"change_id": {"nested": "dict"}}).change_id is None

    def test_non_string_ids_read_as_none(self):
        env = read_envelope({
            "message_id": 123,
            "correlation_id": ["c"],
            "agent_id": {"a": 1},
            "timestamp": 99,
        })
        assert env.message_id is None
        assert env.correlation_id is None
        assert env.agent_id is None
        assert env.timestamp is None

    def test_non_dict_data_is_handled(self):
        """The whole message body being the wrong shape must not raise."""
        assert read_envelope(None).task_type == ""
        assert read_envelope("a string").task_type == ""  # type: ignore[arg-type]
        assert read_envelope([1, 2]).payload == {}  # type: ignore[arg-type]


class TestCertAttemptCoercion:
    def test_int_passes_through(self):
        assert read_envelope({"cert_attempt": 3}).cert_attempt == 3

    def test_digit_string_is_accepted(self):
        """JSON senders vary on numeric encoding, so a digit string is a
        legitimate representation rather than a type error."""
        assert read_envelope({"cert_attempt": "5"}).cert_attempt == 5

    def test_non_numeric_string_reads_as_none(self):
        assert read_envelope({"cert_attempt": "not-a-number"}).cert_attempt is None

    def test_bool_is_rejected_despite_being_an_int_subclass(self):
        """`isinstance(True, int)` is True in Python — without an explicit
        guard, cert_attempt=True would silently become attempt 1."""
        assert read_envelope({"cert_attempt": True}).cert_attempt is None
        assert read_envelope({"cert_attempt": False}).cert_attempt is None


class TestBackwardCompatibilityPreserved:
    def test_legacy_envelope_without_v1_fields_still_parses(self):
        """The whole reason this reader is tolerant: an older NPCI that never
        sends message_id/correlation_id/protocol_version must keep working."""
        env = read_envelope({
            "task_type": "change_communication",
            "payload": {"title": "x"},
            "change_id": "c-1",
        })
        assert env.task_type == "change_communication"
        assert env.payload == {"title": "x"}
        assert env.change_id == "c-1"
        assert env.message_id is None
        assert env.protocol_version is None

    def test_fully_populated_envelope_is_unchanged(self):
        data = {
            "task_type": "query",
            "payload": {"a": 1},
            "from": "npci",
            "message_id": "m1",
            "correlation_id": "corr1",
            "change_id": "ch1",
            "cflow_id": "cf1",
            "cert_attempt": 2,
            "agent_id": "npci.platform.v1",
            "agent_run_id": "run1",
            "timestamp": "2026-01-01T00:00:00Z",
            "protocol_version": "1.0",
        }
        env = read_envelope(data)
        assert env.task_type == "query"
        assert env.payload == {"a": 1}
        assert env.from_ == "npci"
        assert env.message_id == "m1"
        assert env.correlation_id == "corr1"
        assert env.change_id == "ch1"
        assert env.cflow_id == "cf1"
        assert env.cert_attempt == 2
        assert env.agent_id == "npci.platform.v1"
        assert env.agent_run_id == "run1"
        assert env.protocol_version == "1.0"

    def test_empty_envelope_does_not_raise(self):
        env = read_envelope({})
        assert env.task_type == ""
        assert env.payload == {}
