"""Camera-free admission, isolation, and lifecycle checks for AI jobs."""

from copy import deepcopy
import threading
import time
import unittest
from unittest.mock import patch

from driver import ai_provider
from driver.assistant import (Assistant, AssistantError, MAX_CONFIRMATIONS,
                              MAX_RESULTS, RESULT_TTL)
from driver.moves import Move, Waypoint


def shot(count=2):
    points = [Waypoint(f"PRIVATE {index + 1}", 90 + index * 5, -40 + index * 5,
                       duration=10.0)
              for index in range(count)]
    return Move(name="private shot", setup={"notes": "DO NOT SEND"},
                waypoints=points)


def document(move):
    treatments = []
    for duration in (12.0, 15.0):
        beats = [{"index": index, "name": f"P{index + 1}",
                  "duration": waypoint.duration, "dwell": waypoint.dwell,
                  "easing": waypoint.easing}
                 for index, waypoint in enumerate(move.waypoints)]
        beats[-1]["duration"] = duration
        treatments.append({"title": f"Treatment {int(duration)}",
                           "intent": "Change the reveal pacing",
                           "cautions": ["Rehearse locally"], "beats": beats})
    return {"treatments": treatments}


class FakeProvider:
    available = True

    def __init__(self, move, *, blocked=False, error=None, response=None):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()
        self.error = error
        self.response = response or {"document": document(move),
                                     "model": "gpt-5.6-luna",
                                     "usage": {"input_tokens": 10,
                                               "output_tokens": 20,
                                               "total_tokens": 30}}
        self.seen = None

    def generate(self, context, schema, *, model, effort, max_output_tokens):
        self.calls += 1
        self.seen = {"context": deepcopy(context), "schema": deepcopy(schema),
                     "model": model, "effort": effort,
                     "max_output_tokens": max_output_tokens}
        self.started.set()
        if not self.release.wait(3):
            raise RuntimeError("test provider was not released")
        if self.error is not None:
            raise self.error
        response = deepcopy(self.response)
        if response.get("model") == "requested":
            response["model"] = model
        return response


def prepared(service, move, client="browser-a", generation="g1", **kwargs):
    return service.prepare(client, move, generation, "Let the doorway reveal breathe",
                           max_dps=42.0, min_dps=0.3, **kwargs)


def wait_terminal(service, job_id, client="browser-a", generation="g1"):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = service.job(client, job_id, generation)
        if result["state"] not in ("running", "cancelling"):
            return result
        time.sleep(0.005)
    raise AssertionError("assistant job did not finish")


class TestAssistantPreparation(unittest.TestCase):
    def test_revocation_discards_inflight_result_without_refunding_or_retry(self):
        move = shot(); provider = FakeProvider(move, blocked=True)
        service = Assistant(provider, cooldown=0)
        client = 'crew:abc:browser'
        manifest = service.prepare(client, move, 'g1', 'Quiet reveal', max_dps=30, min_dps=.1)
        sent = service.send(client, manifest['confirmation'], 'g1')
        service.revoke_clients('crew:abc:')
        provider.release.set()
        result = wait_terminal(service, sent['job_id'], client=client)
        self.assertEqual(result['state'], 'cancelled')
        self.assertNotIn('result', result)
        self.assertEqual(service.status()['requests_used'], 1)
        self.assertGreater(service.status()['reserved_usd'], 0)

    def test_job_identity_is_known_before_send_and_recoverable_without_redispatch(self):
        move = shot()
        provider = FakeProvider(move)
        service = Assistant(provider, cooldown=0)
        manifest = prepared(service, move)
        sent = service.send("browser-a", manifest["confirmation"], "g1")
        self.assertEqual(sent["job_id"], manifest["job_id"])
        recovered = wait_terminal(service, manifest["job_id"])
        self.assertEqual(recovered["state"], "completed")
        self.assertEqual(provider.calls, 1)

    def test_prepare_is_local_minimal_and_snapshot_isolated(self):
        move = shot()
        provider = FakeProvider(move)
        service = Assistant(provider, cooldown=0)
        manifest = prepared(service, move)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(manifest["context"]["beats"][0]["name"], "P1")
        self.assertNotIn("PRIVATE", str(manifest["context"]))
        self.assertNotIn("DO NOT SEND", str(manifest["context"]))
        self.assertIn("displayed shot context plus fixed instructions", manifest["disclosure"])
        self.assertIn("not an invoice", manifest["disclosure"])
        self.assertGreater(manifest["estimated_ceiling_usd"], 0)
        self.assertEqual(manifest["max_output_tokens"], 3500)
        self.assertGreater(manifest["request_bytes"], 0)
        self.assertEqual(manifest["max_input_tokens_estimate"],
                         manifest["request_bytes"] + 2048)
        before_hash = manifest["snapshot_hash"]
        move.waypoints[-1].duration = 99
        self.assertEqual(manifest["snapshot_hash"], before_hash)
        manifest["context"]["brief"] = "mutated by caller"
        job_id = service.send("browser-a", manifest["confirmation"], "g1")["job_id"]
        wait_terminal(service, job_id)
        self.assertEqual(provider.seen["context"]["brief"],
                         "Let the doorway reveal breathe")

    def test_24_beats_get_bounded_output_capacity(self):
        move = shot(24)
        manifest = prepared(Assistant(FakeProvider(move)), move)
        self.assertEqual(manifest["max_output_tokens"], 6000)
        self.assertGreater(manifest["estimated_ceiling_usd"], 0)

    def test_owner_model_effort_and_constructor_bounds(self):
        move = shot()
        service = Assistant(FakeProvider(move))
        for client in ("", "x" * 129, "bad\nowner"):
            with self.subTest(client=repr(client)), self.assertRaises(AssistantError):
                prepared(service, move, client=client)
        for options in ({"model": "unknown"}, {"effort": "maximum"},
                        {"model": []}, {"effort": []}):
            with self.assertRaises(AssistantError):
                prepared(service, move, **options)
        for kwargs in ({"request_limit": 21}, {"request_limit": True},
                       {"budget_usd": 0}, {"cooldown": -1},
                       {"budget_usd": 10 ** 1000}):
            with self.assertRaises(AssistantError):
                Assistant(None, **kwargs)

    def test_safe_source_validation_remains_actionable(self):
        move = shot(1)
        with self.assertRaisesRegex(AssistantError, "2 to 24"):
            prepared(Assistant(FakeProvider(move)), move)

    def test_confirmations_are_bounded_and_expire(self):
        move = shot()
        service = Assistant(FakeProvider(move))
        manifests = [prepared(service, move, generation=f"g{index}")
                     for index in range(MAX_CONFIRMATIONS)]
        with self.assertRaisesRegex(AssistantError, "Too many"):
            prepared(service, move, generation="overflow")
        nonce = manifests[0]["confirmation"]
        with service._lock:
            service._confirmations[nonce]["expires_at"] = 0
        self.assertEqual(service.status()["pending_confirmations"],
                         MAX_CONFIRMATIONS - 1)
        with self.assertRaisesRegex(AssistantError, "expired"):
            service.send("browser-a", nonce, "g0")


class TestAssistantAdmission(unittest.TestCase):
    def test_wrong_owner_cannot_burn_nonce_and_valid_send_is_single_use(self):
        move = shot()
        provider = FakeProvider(move, blocked=True)
        service = Assistant(provider, cooldown=0)
        manifest = prepared(service, move)
        with self.assertRaisesRegex(AssistantError, "another browser"):
            service.send("browser-b", manifest["confirmation"], "g1")
        job_id = service.send("browser-a", manifest["confirmation"], "g1")["job_id"]
        self.assertTrue(provider.started.wait(1))
        with self.assertRaisesRegex(AssistantError, "invalid or expired"):
            service.send("browser-a", manifest["confirmation"], "g1")
        provider.release.set()
        wait_terminal(service, job_id)

    def test_stale_generation_refused_and_consumes_confirmation(self):
        move = shot()
        provider = FakeProvider(move)
        service = Assistant(provider)
        manifest = prepared(service, move)
        with self.assertRaisesRegex(AssistantError, "Draft changed"):
            service.send("browser-a", manifest["confirmation"], "g2")
        with self.assertRaisesRegex(AssistantError, "invalid or expired"):
            service.send("browser-a", manifest["confirmation"], "g1")
        self.assertEqual(provider.calls, 0)

    def test_cancel_keeps_host_busy_discards_late_result_and_never_refunds(self):
        move = shot()
        provider = FakeProvider(move, blocked=True)
        service = Assistant(provider, cooldown=0)
        manifest = prepared(service, move)
        job_id = service.send("browser-a", manifest["confirmation"], "g1")["job_id"]
        self.assertTrue(provider.started.wait(1))
        reserved = service.status()["reserved_usd"]
        self.assertEqual(service.cancel("browser-a", job_id)["state"], "cancelling")
        self.assertTrue(service.status()["busy"])
        other = Assistant(FakeProvider(move), cooldown=0)
        other_manifest = prepared(other, move, client="browser-b")
        with self.assertRaisesRegex(AssistantError, "still running"):
            other.send("browser-b", other_manifest["confirmation"], "g1")
        provider.release.set()
        result = wait_terminal(service, job_id)
        self.assertEqual(result, {"job_id": job_id, "state": "cancelled"})
        self.assertFalse(service.status()["busy"])
        self.assertEqual(service.status()["reserved_usd"], reserved)
        self.assertEqual(provider.calls, 1)

    def test_request_limit_is_lifetime_and_cooldown_is_after_start(self):
        move = shot()
        provider = FakeProvider(move)
        service = Assistant(provider, request_limit=1, cooldown=60)
        first = prepared(service, move)
        job_id = service.send("browser-a", first["confirmation"], "g1")["job_id"]
        wait_terminal(service, job_id)
        second = prepared(service, move, generation="g2")
        with self.assertRaisesRegex(AssistantError, "request limit"):
            service.send("browser-a", second["confirmation"], "g2")
        self.assertGreater(service.status()["cooldown_remaining"], 0)
        self.assertEqual(provider.calls, 1)

    def test_budget_refusal_does_not_dispatch(self):
        move = shot()
        provider = FakeProvider(move)
        service = Assistant(provider, budget_usd=0.000001, cooldown=0)
        manifest = prepared(service, move, model="gpt-6-astra")
        with self.assertRaisesRegex(AssistantError, "budget"):
            service.send("browser-a", manifest["confirmation"], "g1")
        self.assertEqual(provider.calls, 0)
        self.assertEqual(service.status()["requests_used"], 0)

    def test_unavailable_provider_is_refused_before_reservation(self):
        move = shot()
        provider = FakeProvider(move)
        provider.available = False
        service = Assistant(provider, cooldown=0)
        manifest = prepared(service, move)
        with self.assertRaisesRegex(AssistantError, "not configured"):
            service.send("browser-a", manifest["confirmation"], "g1")
        self.assertEqual(service.status()["reserved_usd"], 0)
        self.assertEqual(provider.calls, 0)


class TestAssistantResults(unittest.TestCase):
    def test_completed_result_is_owned_fresh_strict_and_candidate_is_a_copy(self):
        move = shot()
        provider = FakeProvider(move)
        service = Assistant(provider, cooldown=0)
        manifest = prepared(service, move)
        job_id = service.send("browser-a", manifest["confirmation"], "g1")["job_id"]
        result = wait_terminal(service, job_id)
        self.assertEqual(set(result["result"]),
                         {"treatments", "model", "usage", "estimated_ceiling_usd"})
        with service._lock:
            self.assertFalse({"context", "schema", "snapshot"}
                             & service._jobs[job_id].keys())
        with self.assertRaisesRegex(AssistantError, "another browser"):
            service.job("browser-b", job_id, "g1")
        with self.assertRaisesRegex(AssistantError, "stale"):
            service.job("browser-a", job_id, "g2")
        candidate = service.candidate("browser-a", job_id, 0, "g1")
        original_name = candidate["name"]
        candidate["name"] = "caller mutation"
        again = service.candidate("browser-a", job_id, 0, "g1")
        self.assertEqual(again["name"], original_name)
        with self.assertRaisesRegex(AssistantError, "index"):
            service.candidate("browser-a", job_id, True, "g1")
        self.assertEqual(service.cancel("browser-a", job_id)["state"], "cancelled")
        self.assertEqual(service.job("browser-a", job_id, "g1"),
                         {"job_id": job_id, "state": "cancelled"})
        with self.assertRaisesRegex(AssistantError, "no completed"):
            service.candidate("browser-a", job_id, 0, "g1")

    def test_failed_preflight_candidate_is_never_returned(self):
        move = shot()
        provider = FakeProvider(move)
        service = Assistant(provider, cooldown=0)
        manifest = service.prepare("browser-a", move, "g1", "Faster",
                                   max_dps=0.3, min_dps=0.1)
        job_id = service.send("browser-a", manifest["confirmation"], "g1")["job_id"]
        self.assertEqual(wait_terminal(service, job_id)["state"], "completed")
        with self.assertRaisesRegex(AssistantError, "preflight"):
            service.candidate("browser-a", job_id, 0, "g1")

    def test_provider_error_is_safe_unknown_error_is_sanitized_and_no_retry(self):
        move = shot()
        for error, expected, forbidden in (
                (ai_provider.ProviderError("OpenAI unavailable. No retry was made."),
                 "No retry", "secret"),
                (RuntimeError("secret-token-123"), "safely used", "secret-token-123")):
            provider = FakeProvider(move, error=error)
            service = Assistant(provider, cooldown=0)
            manifest = prepared(service, move)
            job_id = service.send("browser-a", manifest["confirmation"], "g1")["job_id"]
            result = wait_terminal(service, job_id)
            self.assertEqual(result["state"], "failed")
            self.assertIn(expected, result["error"])
            self.assertNotIn(forbidden, result["error"])
            self.assertEqual(provider.calls, 1)
            self.assertGreater(service.status()["reserved_usd"], 0)
            self.assertEqual(service.cancel("browser-a", job_id)["state"], "cancelled")
            self.assertNotIn("error", service.job("browser-a", job_id, "g1"))

    def test_malformed_provider_envelope_and_usage_are_rejected(self):
        move = shot()
        bad_responses = [
            {"document": document(move), "model": "requested", "usage": None,
             "raw": "unexpected"},
            {"document": document(move), "model": "requested",
             "usage": {"input_tokens": 1, "output_tokens": -1,
                       "total_tokens": 0}},
        ]
        for response in bad_responses:
            provider = FakeProvider(move, response=response)
            service = Assistant(provider, cooldown=0)
            manifest = prepared(service, move)
            job_id = service.send("browser-a", manifest["confirmation"], "g1")["job_id"]
            result = wait_terminal(service, job_id)
            self.assertEqual(result["state"], "failed")
            self.assertNotIn("raw", result)
            self.assertEqual(provider.calls, 1)

    def test_unforeseen_worker_setup_error_releases_host_guard(self):
        move = shot()
        provider = FakeProvider(move)
        service = Assistant(provider, cooldown=0)
        manifest = prepared(service, move)
        with patch("driver.assistant._copy_move", side_effect=RuntimeError("secret")):
            job_id = service.send("browser-a", manifest["confirmation"], "g1")["job_id"]
            result = wait_terminal(service, job_id)
        self.assertEqual(result["state"], "failed")
        self.assertNotIn("secret", result["error"])
        self.assertFalse(service.status()["busy"])

    def test_completed_result_expires_and_retention_is_bounded(self):
        move = shot()
        service = Assistant(FakeProvider(move), cooldown=0)
        manifest = prepared(service, move)
        job_id = service.send("browser-a", manifest["confirmation"], "g1")["job_id"]
        wait_terminal(service, job_id)
        with service._lock:
            service._jobs[job_id]["finished_at"] = 0
        # Monotonic zero is a valid finish time, not a missing timestamp.
        # Fresh CI machines may have less uptime than RESULT_TTL: never use
        # the host's uptime as an implicit expiry fixture.
        with patch("driver.assistant.time.monotonic", return_value=RESULT_TTL - 0.001):
            self.assertEqual(service.job("browser-a", job_id, "g1")["state"], "completed")
        with patch("driver.assistant.time.monotonic", return_value=RESULT_TTL):
            with self.assertRaisesRegex(AssistantError, "not found or expired"):
                service.job("browser-a", job_id, "g1")
        with service._lock:
            for index in range(MAX_RESULTS + 1):
                service._jobs[f"synthetic-{index}"] = {
                    "finished_at": float(index + 1)}
            service._prune_locked(100.0)
            self.assertEqual(len(service._jobs), MAX_RESULTS)
            self.assertNotIn("synthetic-0", service._jobs)


if __name__ == "__main__":
    unittest.main()
