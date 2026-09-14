"""Bounded asynchronous service for optional, data-only shot assistance.

The service owns request admission and result lifetime.  It never owns the
camera, workspace persistence, or application of a candidate move.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import math
import re
import secrets
import threading
import time

from . import ai_provider, copilot
from .moves import Move


MIN_OUTPUT_TOKENS = 3500
MAX_OUTPUT_TOKENS = 6000
MAX_REQUEST_BYTES = 48_000
CONFIRMATION_TTL = 120.0
RESULT_TTL = 20.0 * 60.0
MAX_CONFIRMATIONS = 32
MAX_RESULTS = 20
MAX_CLIENT_LENGTH = 128
MAX_GENERATION_LENGTH = 256

MODEL_RATES = {
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-sol": (4.00, 20.00),
    "gpt-6-astra": (10.00, 50.00),
}
EFFORTS = frozenset(("low", "medium", "high"))
ESTIMATE_NOTE = (
    "Conservative admission ceiling using public rates dated 2026-09-14; "
    "it is not an invoice or a provider billing guarantee."
)
DISCLOSURE = (
    "The displayed shot context plus fixed instructions and a response schema "
    "will be sent to OpenAI after confirmation. "
    "The assistant has no camera authority and cannot apply a draft. "
    + ESTIMATE_NOTE
)


class AssistantError(Exception):
    """Sanitized service error suitable for the local operator UI."""


def _number(value, name: str, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float):
        raise AssistantError(f"{name} is out of range")
    try:
        value = float(value)
    except (OverflowError, ValueError):
        raise AssistantError(f"{name} is out of range") from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise AssistantError(f"{name} is out of range")
    return value


def _identity(value, name: str, maximum: int) -> str:
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise AssistantError(f"Invalid {name}")
    return value


def _copy_move(move: Move) -> Move:
    if not isinstance(move, Move):
        raise AssistantError("Assistant requires an authored move")
    try:
        return Move.from_dict(deepcopy(move.to_dict()))
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise AssistantError("Assistant requires a valid authored move") from None


def _snapshot_hash(move: Move) -> str:
    try:
        encoded = json.dumps(move.to_dict(), sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise AssistantError("Assistant requires a valid authored move") from None
    return hashlib.sha256(encoded).hexdigest()


def _output_tokens(beat_count: int) -> int:
    return min(MAX_OUTPUT_TOKENS, max(MIN_OUTPUT_TOKENS, 1000 + 220 * beat_count))


def _ceiling(payload_size: int, model: str, output_tokens: int) -> float:
    input_rate, output_rate = MODEL_RATES[model]
    # Treat each request byte as a token, add a fixed instruction/envelope
    # margin, and price all input at the cache-write multiplier.  This is
    # deliberately much more conservative than a tokenizer estimate.
    input_bound = payload_size + 2048
    cost = ((input_bound * input_rate * 1.25)
            + (output_tokens * output_rate)) / 1_000_000.0
    return max(0.000001, math.ceil(cost * 1_000_000.0) / 1_000_000.0)


class Assistant:
    """One-request-at-a-time, in-memory assistant job service."""

    _host_request = threading.Lock()

    def __init__(self, provider=None, *, budget_usd=1.0,
                 request_limit=20, cooldown=10.0):
        self._budget = _number(budget_usd, "budget", 0.000001, 100.0)
        if type(request_limit) is not int or not 1 <= request_limit <= 20:
            raise AssistantError("request limit is out of range")
        self._request_limit = request_limit
        self._cooldown = _number(cooldown, "cooldown", 0.0, 3600.0)
        self._provider = provider
        self._lock = threading.Lock()
        self._confirmations: OrderedDict[str, dict] = OrderedDict()
        self._jobs: OrderedDict[str, dict] = OrderedDict()
        self._busy_job_id: str | None = None
        self._requests_used = 0
        self._reserved = 0.0
        self._last_started: float | None = None

    def _prune_locked(self, now: float) -> None:
        for nonce, record in list(self._confirmations.items()):
            if record["expires_at"] <= now:
                del self._confirmations[nonce]
        for job_id, record in list(self._jobs.items()):
            finished = record.get("finished_at")
            if finished is not None and finished + RESULT_TTL <= now:
                del self._jobs[job_id]
        terminal = [(job_id, record["finished_at"])
                    for job_id, record in self._jobs.items()
                    if record.get("finished_at") is not None]
        terminal.sort(key=lambda item: item[1])
        while len(terminal) > MAX_RESULTS:
            job_id, _ = terminal.pop(0)
            self._jobs.pop(job_id, None)

    def _provider_available(self) -> bool:
        if self._provider is None:
            return False
        try:
            marker = getattr(self._provider, "available", True)
        except Exception:
            return False
        return marker is True

    def configure(self, update):
        """Apply local host configuration without resetting admission accounting.

        The callback persists validated settings and returns (provider, budget,
        limit). Holding the admission lock prevents a Send/configuration race.
        Prepared disclosures become invalid because their limits may change.
        """
        with self._lock:
            if self._busy_job_id is not None or Assistant._host_request.locked():
                raise AssistantError('Wait for the current AI request before changing settings')
            provider, budget, limit = update()
            budget = _number(budget, 'budget', 0.000001, 100.0)
            if type(limit) is not int or not 1 <= limit <= 20:
                raise AssistantError('request limit is out of range')
            self._provider, self._budget, self._request_limit = provider, budget, limit
            self._confirmations.clear()

    def status(self) -> dict:
        now = time.monotonic()
        available = self._provider_available()
        with self._lock:
            self._prune_locked(now)
            remaining_cooldown = (0.0 if self._last_started is None else
                                  max(0.0, self._cooldown - (now - self._last_started)))
            return {
                "available": available,
                "busy": Assistant._host_request.locked(),
                "requests_used": self._requests_used,
                "request_limit": self._request_limit,
                "reserved_usd": round(self._reserved, 6),
                "remaining_budget_usd": round(max(0.0, self._budget - self._reserved), 6),
                "cooldown_remaining": round(remaining_cooldown, 3),
                "pending_confirmations": len(self._confirmations),
                "retained_results": sum(record.get("finished_at") is not None
                                        for record in self._jobs.values()),
                "output_token_range": [MIN_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS],
                "estimate_note": ESTIMATE_NOTE,
                "rates_usd_per_million": {
                    name: {"input": rates[0], "output": rates[1]}
                    for name, rates in MODEL_RATES.items()
                },
            }

    def prepare(self, client: str, move: Move, generation: str, brief: str, *,
                include_labels=False, model="gpt-5.6-luna", effort="low",
                max_dps: float, min_dps: float) -> dict:
        client = _identity(client, "client", MAX_CLIENT_LENGTH)
        generation = _identity(generation, "generation", MAX_GENERATION_LENGTH)
        if (not isinstance(model, str) or model not in MODEL_RATES
                or not isinstance(effort, str) or effort not in EFFORTS):
            raise AssistantError("Unsupported model or reasoning effort")
        max_dps = _number(max_dps, "maximum speed", 0.000001, 100_000.0)
        min_dps = _number(min_dps, "minimum speed", 0.000001, max_dps)
        snapshot = _copy_move(move)
        try:
            context = copilot.build_context(snapshot, brief, include_labels=include_labels)
            schema = copilot.response_schema(len(snapshot.waypoints))
            output_tokens = _output_tokens(len(snapshot.waypoints))
            payload = ai_provider.request_payload(
                context, schema, model, effort, output_tokens)
        except ai_provider.ProviderError as exc:
            raise AssistantError(str(exc)) from None
        except ValueError as exc:
            # copilot's validation messages are bounded, value-free, and tell
            # the operator whether the brief or 2..24 beat source must change.
            raise AssistantError(str(exc)) from None
        except (TypeError, OverflowError, RecursionError):
            raise AssistantError("Assistant request is invalid") from None
        if not isinstance(payload, bytes) or len(payload) > MAX_REQUEST_BYTES:
            raise AssistantError("Assistant request is too large. Nothing sent.")
        estimate = _ceiling(len(payload), model, output_tokens)
        now = time.monotonic()
        nonce = secrets.token_urlsafe(24)
        with self._lock:
            self._prune_locked(now)
            if len(self._confirmations) >= MAX_CONFIRMATIONS:
                raise AssistantError("Too many pending confirmations")
            while nonce in self._confirmations or nonce in self._jobs:
                nonce = secrets.token_urlsafe(24)
            self._confirmations[nonce] = {
                "client": client,
                "generation": generation,
                "snapshot": snapshot,
                "context": deepcopy(context),
                "schema": deepcopy(schema),
                "model": model,
                "effort": effort,
                "max_dps": max_dps,
                "min_dps": min_dps,
                "estimated_ceiling_usd": estimate,
                "max_output_tokens": output_tokens,
                "expires_at": now + CONFIRMATION_TTL,
            }
        return {
            "confirmation": nonce,
            # The browser knows the future ID before a billable send. A lost
            # admission response can be recovered by read-only job polling.
            "job_id": nonce,
            "context": deepcopy(context),
            "generation": generation,
            "snapshot_hash": _snapshot_hash(snapshot),
            "model": model,
            "effort": effort,
            "estimated_ceiling_usd": estimate,
            "max_output_tokens": output_tokens,
            "request_bytes": len(payload),
            "max_input_tokens_estimate": len(payload) + 2048,
            "expires_in_seconds": int(CONFIRMATION_TTL),
            "disclosure": DISCLOSURE,
        }

    def send(self, client: str, confirmation: str, generation: str) -> dict:
        client = _identity(client, "client", MAX_CLIENT_LENGTH)
        confirmation = _identity(confirmation, "confirmation", 256)
        generation = _identity(generation, "generation", MAX_GENERATION_LENGTH)
        now = time.monotonic()
        provider_available = self._provider_available()
        acquired_host = False
        with self._lock:
            self._prune_locked(now)
            prepared = self._confirmations.get(confirmation)
            if prepared is None:
                raise AssistantError("Confirmation is invalid or expired")
            if prepared["client"] != client:
                raise AssistantError("Confirmation belongs to another browser")
            # A nonce presented by its owner is spent even if admission fails.
            del self._confirmations[confirmation]
            if prepared["expires_at"] <= now:
                raise AssistantError("Confirmation is invalid or expired")
            if prepared["generation"] != generation:
                raise AssistantError("Draft changed; prepare the request again")
            if not provider_available:
                raise AssistantError("AI provider is not configured")
            if self._requests_used >= self._request_limit:
                raise AssistantError("Assistant request limit reached")
            if (self._last_started is not None
                    and now - self._last_started < self._cooldown):
                raise AssistantError("Assistant cooldown is active")
            estimate = prepared["estimated_ceiling_usd"]
            if self._reserved + estimate > self._budget + 1e-12:
                raise AssistantError("Assistant budget ceiling reached")
            if not Assistant._host_request.acquire(blocking=False):
                raise AssistantError("Another assistant request is still running")
            acquired_host = True
            job_id = confirmation
            self._requests_used += 1
            self._reserved += estimate
            self._last_started = now
            self._busy_job_id = job_id
            self._jobs[job_id] = {
                **prepared,
                "job_id": job_id,
                "state": "running",
                "cancel_requested": False,
                "started_at": now,
                "finished_at": None,
            }
        thread = threading.Thread(target=self._run, args=(job_id,),
                                  name="osmo-assistant", daemon=True)
        try:
            thread.start()
        except Exception:
            with self._lock:
                record = self._jobs[job_id]
                record["state"] = "failed"
                record["error"] = "Assistant worker could not start"
                record["finished_at"] = time.monotonic()
                self._busy_job_id = None
                self._requests_used -= 1
                self._reserved -= estimate
            if acquired_host:
                Assistant._host_request.release()
            raise AssistantError("Assistant worker could not start") from None
        return {"job_id": job_id}

    @staticmethod
    def _usage(value):
        if value is None:
            return None
        fields = ("input_tokens", "output_tokens", "total_tokens")
        if (not isinstance(value, dict) or set(value) != set(fields)
                or any(type(value.get(field)) is not int or value[field] < 0
                       for field in fields)):
            raise ValueError("invalid usage")
        return {field: value[field] for field in fields}

    def _run(self, job_id: str) -> None:
        state = "failed"
        result = None
        error = "Assistant request failed. Draft unchanged."
        try:
            try:
                with self._lock:
                    record = self._jobs[job_id]
                    if record['cancel_requested']:
                        raise AssistantError('Request discarded before provider processing')
                    context = deepcopy(record["context"])
                    schema = deepcopy(record["schema"])
                    snapshot = _copy_move(record["snapshot"])
                    model, effort = record["model"], record["effort"]
                    max_dps, min_dps = record["max_dps"], record["min_dps"]
                    estimate = record["estimated_ceiling_usd"]
                    output_tokens = record["max_output_tokens"]
                response = self._provider.generate(
                    context, schema, model=model, effort=effort,
                    max_output_tokens=output_tokens)
                if (not isinstance(response, dict)
                        or set(response) != {"document", "model", "usage"}):
                    raise ValueError("invalid provider envelope")
                actual_model = response["model"]
                if (not isinstance(actual_model, str) or not re.fullmatch(
                        re.escape(model) + r"(?:-\d{4}-\d{2}-\d{2})?", actual_model)):
                    raise ValueError("invalid provider model")
                usage = self._usage(response["usage"])
                treatments = copilot.validate_treatments(
                    snapshot, response["document"], max_dps=max_dps, min_dps=min_dps)
                result = {"treatments": treatments, "model": actual_model,
                          "usage": usage, "estimated_ceiling_usd": estimate}
                state = "completed"
            except ai_provider.ProviderError as exc:
                error = str(exc) or "Assistant request failed. Draft unchanged."
            except Exception:
                # Do not log or retain provider exceptions or raw output.
                error = "Assistant response could not be safely used. Draft unchanged."
            finished = time.monotonic()
            with self._lock:
                record = self._jobs.get(job_id)
                if record is not None:
                    if record["cancel_requested"]:
                        record["state"] = "cancelled"
                    elif state == "completed":
                        record["state"] = "completed"
                        record["result"] = result
                    else:
                        record["state"] = "failed"
                        record["error"] = error
                    record["finished_at"] = finished
                    for field in ("context", "schema", "snapshot"):
                        record.pop(field, None)
                    self._busy_job_id = None
                    self._prune_locked(finished)
        finally:
            Assistant._host_request.release()

    def job(self, client: str, job_id: str, generation: str) -> dict:
        client = _identity(client, "client", MAX_CLIENT_LENGTH)
        job_id = _identity(job_id, "job id", 256)
        generation = _identity(generation, "generation", MAX_GENERATION_LENGTH)
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            record = self._jobs.get(job_id)
            if record is None:
                raise AssistantError("Assistant job was not found or expired")
            if record["client"] != client:
                raise AssistantError("Assistant job belongs to another browser")
            if record["generation"] != generation:
                raise AssistantError("Assistant result is stale for this draft")
            public = {"job_id": job_id, "state": record["state"]}
            if record["state"] == "completed":
                public["result"] = deepcopy(record["result"])
            elif record["state"] == "failed":
                public["error"] = record["error"]
            return public

    def cancel(self, client: str, job_id: str) -> dict:
        client = _identity(client, "client", MAX_CLIENT_LENGTH)
        job_id = _identity(job_id, "job id", 256)
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            record = self._jobs.get(job_id)
            if record is None:
                raise AssistantError("Assistant job was not found or expired")
            if record["client"] != client:
                raise AssistantError("Assistant job belongs to another browser")
            if record["state"] in ("running", "cancelling"):
                record["cancel_requested"] = True
                record["state"] = "cancelling"
            elif record["state"] != "cancelled":
                record.pop("result", None)
                record.pop("error", None)
                record["state"] = "cancelled"
            return {"job_id": job_id, "state": record["state"]}

    def revoke_clients(self, prefix):
        """Discard a revoked crew member's disclosures/results, without a retry.

        Provider IO already in flight cannot be recalled or unbilled. Its late
        response is discarded; the reservation remains counted for this host.
        """
        with self._lock:
            for nonce, record in list(self._confirmations.items()):
                if record['client'].startswith(prefix):
                    del self._confirmations[nonce]
            for record in self._jobs.values():
                if record['client'].startswith(prefix):
                    record['cancel_requested'] = True
                    if record['state'] in ('running', 'cancelling'):
                        record['state'] = 'cancelling'
                    else:
                        record['state'] = 'cancelled'
                        record.pop('result', None)
                        record.pop('error', None)

    def candidate(self, client: str, job_id: str, index: int,
                  generation: str) -> dict:
        if type(index) is not int:
            raise AssistantError("Candidate index is invalid")
        completed = self.job(client, job_id, generation)
        if completed["state"] != "completed":
            raise AssistantError("Assistant job has no completed candidate")
        treatments = completed["result"]["treatments"]
        if not 0 <= index < len(treatments):
            raise AssistantError("Candidate index is invalid")
        treatment = treatments[index]
        assessment = treatment.get("assessment")
        if (not isinstance(assessment, dict)
                or not isinstance(assessment.get("preflight"), dict)
                or assessment["preflight"].get("ok") is not True):
            raise AssistantError("Candidate did not pass local preflight")
        candidate = deepcopy(treatment.get("move"))
        try:
            Move.from_dict(candidate)
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise AssistantError("Candidate is invalid") from None
        return candidate
