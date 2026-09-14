"""Camera-free checks for untrusted creative proposals and outbound privacy."""

from copy import deepcopy
import json
import unittest

from driver import copilot, director, moves


def shot():
    return moves.Move(name="private shot", loop=False, ping_pong=True,
                      setup={"mount": "tripod", "notes": "PRIVATE NOTES"},
                      waypoints=[
                          moves.Waypoint("PRIVATE FIRST", 90, 0, duration=7, cue=True, zoom=.2),
                          moves.Waypoint("PRIVATE LAST", 110, 15, duration=8, dwell=1,
                                         flow=True, zoom=.8, zoom_easing="linear")])


def document(move=None):
    move = move or shot()
    treatments = []
    for duration in (10, 14):
        beats = [{"index": i, "name": f"P{i + 1}", "duration": w.duration,
                  "dwell": w.dwell, "easing": w.easing}
                 for i, w in enumerate(move.waypoints)]
        beats[1]["duration"] = duration
        treatments.append({"title": "Reveal", "intent": "Hold the payoff",
                           "cautions": ["Rehearse on the rig"], "beats": beats})
    return {"treatments": treatments}


class TestCopilotContext(unittest.TestCase):
    def test_minimal_context_excludes_private_fields_and_coordinates(self):
        context = copilot.build_context(shot(), "  Reveal the doorway  ")
        self.assertEqual(context["brief"], "Reveal the doorway")
        self.assertEqual(set(context), {"brief", "loop", "ping_pong", "cue_uncertain", "allowed_easings", "beats"})
        self.assertNotIn("PRIVATE", json.dumps(context))
        self.assertNotIn("private shot", json.dumps(context))
        self.assertEqual(set(context["beats"][0]),
                         {"index", "name", "duration", "dwell", "easing", "cue", "leg_distance_deg"})
        self.assertEqual(context["beats"][0]["name"], "P1")
        self.assertEqual(context["beats"][1]["leg_distance_deg"], {"pan": 15., "tilt": 20.})

    def test_label_sharing_is_explicit(self):
        self.assertEqual(copilot.build_context(shot(), "Reveal", include_labels=True)
                         ["beats"][0]["name"], "PRIVATE FIRST")
        with self.assertRaises(ValueError):
            copilot.build_context(shot(), "Reveal", include_labels="false")

    def test_brief_and_count_bounds(self):
        for brief in (None, True, "", "  ", "x" * 1601):
            with self.subTest(brief_type=type(brief)), self.assertRaises(ValueError):
                copilot.build_context(shot(), brief)
        self.assertEqual(len(copilot.build_context(shot(), " " + "x" * 1600 + " ")["brief"]), 1600)
        for count in (0, 1, 25):
            move = shot()
            move.waypoints = [move.waypoints[0]] * count
            with self.assertRaises(ValueError):
                copilot.build_context(move, "Reveal")

    def test_schema_is_closed_and_bounded(self):
        schema = copilot.response_schema(2)
        self.assertFalse(schema["additionalProperties"])
        treatments = schema["properties"]["treatments"]
        self.assertEqual((treatments["minItems"], treatments["maxItems"]), (2, 2))
        beats = treatments["items"]["properties"]["beats"]
        self.assertFalse(beats["items"]["additionalProperties"])
        self.assertEqual(set(beats["items"]["properties"]), copilot.BEAT_FIELDS)
        for count in (True, 1, 25, 2.0):
            with self.assertRaises(ValueError):
                copilot.response_schema(count)


class TestTreatmentValidation(unittest.TestCase):
    def test_candidates_preserve_geometry_and_original_and_have_truthful_assessment(self):
        move = shot()
        before = deepcopy(move.to_dict())
        incoming = document(move)
        incoming_before = deepcopy(incoming)
        results = copilot.validate_treatments(move, incoming, max_dps=1)
        self.assertEqual(move.to_dict(), before)
        self.assertEqual(incoming, incoming_before)
        for result in results:
            candidate = result["move"]
            for key in before.keys() - {"waypoints"}:
                self.assertEqual(candidate[key], before[key])
            for original, proposed in zip(before["waypoints"], candidate["waypoints"]):
                for key in original.keys() - {"name", "duration", "dwell", "easing"}:
                    self.assertEqual(proposed[key], original[key])
            canonical = director.preview(moves.Move.from_dict(candidate), max_dps=1)
            self.assertEqual(result["assessment"], canonical)
            self.assertFalse(result["assessment"]["preflight"]["ok"])
            self.assertIn({"index": 1, "field": "duration", "before": 8,
                           "after": candidate["waypoints"][1]["duration"]}, result["diff"])
        results[0]["move"]["setup"]["notes"] = "changed"
        self.assertEqual(move.setup["notes"], "PRIVATE NOTES")
        self.assertEqual(results[1]["move"]["setup"]["notes"], "PRIVATE NOTES")

    def test_first_duration_preserved_and_not_reported_as_change(self):
        incoming = document()
        result = copilot.validate_treatments(shot(), incoming)[0]
        self.assertEqual(result["move"]["waypoints"][0]["duration"], 7)
        self.assertFalse(any(d["index"] == 0 and d["field"] == "duration" for d in result["diff"]))
        for field, value in (("duration", 30), ("easing", "linear")):
            incoming = document()
            incoming["treatments"][0]["beats"][0][field] = value
            with self.assertRaisesRegex(ValueError, "First beat"):
                copilot.validate_treatments(shot(), incoming)

    def test_missing_extra_and_malicious_fields_rejected(self):
        for layer in ("document", "treatment", "beat"):
            for remove in (False, True):
                incoming = document()
                node = incoming if layer == "document" else incoming["treatments"][0]
                if layer == "beat":
                    node = node["beats"][0]
                if remove:
                    node.pop(next(iter(node)))
                else:
                    node["pitch"] = 0
                with self.subTest(layer=layer, remove=remove), self.assertRaises(ValueError):
                    copilot.validate_treatments(shot(), incoming)
        for field in ("cue", "flow", "zoom", "yaw", "setup", "command"):
            incoming = document()
            incoming["treatments"][0]["beats"][0][field] = False
            with self.subTest(field=field), self.assertRaises(ValueError):
                copilot.validate_treatments(shot(), incoming)

    def test_numeric_types_nonfinite_and_bounds(self):
        for field, values in {"duration": [True, "3", None, float("nan"), float("inf"), -.1, .049, 3601, 10**1000],
                              "dwell": [False, "0", None, float("nan"), float("-inf"), -.01, 3601]}.items():
            for value in values:
                incoming = document()
                incoming["treatments"][0]["beats"][1][field] = value
                with self.subTest(field=field, value_type=type(value)), self.assertRaises(ValueError):
                    copilot.validate_treatments(shot(), incoming)

    def test_boundary_numbers_accepted_without_normalizing_original_geometry(self):
        move = shot()
        move.waypoints[0].pitch = 450.0
        incoming = document(move)
        incoming["treatments"][0]["beats"][1].update(duration=.05, dwell=0)
        incoming["treatments"][1]["beats"][1].update(duration=3600, dwell=3600)
        results = copilot.validate_treatments(move, incoming)
        self.assertEqual(results[0]["move"]["waypoints"][0]["pitch"], 450.0)
        self.assertEqual(results[0]["move"]["waypoints"][1]["duration"], .05)
        self.assertEqual(results[1]["move"]["waypoints"][1]["dwell"], 3600)

    def test_wrong_container_types_rejected(self):
        for incoming in (None, [], {"treatments": {}}, {"treatments": [None, None]}):
            with self.assertRaises(ValueError):
                copilot.validate_treatments(shot(), incoming)
        incoming = document()
        incoming["treatments"][0]["beats"] = {}
        with self.assertRaises(ValueError):
            copilot.validate_treatments(shot(), incoming)

    def test_count_order_and_easing(self):
        for value in (True, 1, -1, "0", .0):
            incoming = document()
            incoming["treatments"][0]["beats"][0]["index"] = value
            with self.assertRaises(ValueError):
                copilot.validate_treatments(shot(), incoming)
        for value in ("unknown", [], True):
            incoming = document()
            incoming["treatments"][0]["beats"][0]["easing"] = value
            with self.assertRaises(ValueError):
                copilot.validate_treatments(shot(), incoming)
        for target in ("treatments", "beats"):
            incoming = document()
            array = incoming["treatments"] if target == "treatments" else incoming["treatments"][0]["beats"]
            array.pop()
            with self.assertRaises(ValueError):
                copilot.validate_treatments(shot(), incoming)

    def test_noop_names_only_first_leg_only_and_duplicate_pacing(self):
        for kind in ("noop", "names", "first", "duplicate"):
            incoming = document()
            if kind == "duplicate":
                incoming["treatments"][1]["beats"] = deepcopy(incoming["treatments"][0]["beats"])
            else:
                incoming["treatments"][0]["beats"][1]["duration"] = 8
                if kind == "first":
                    incoming["treatments"][0]["beats"][0].update(duration=20, easing="linear")
                if kind == "noop":
                    for beat, waypoint in zip(incoming["treatments"][0]["beats"], shot().waypoints):
                        beat["name"] = waypoint.name
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                copilot.validate_treatments(shot(), incoming)

    def test_text_bounds_and_safe_errors(self):
        for field, value in (("title", "x" * 65), ("intent", "x" * 501),
                             ("intent", False), ("cautions", ["x"] * 5),
                             ("cautions", ["x" * 241]), ("cautions", "note")):
            incoming = document()
            incoming["treatments"][0][field] = value
            with self.assertRaises(ValueError) as failure:
                copilot.validate_treatments(shot(), incoming)
            self.assertNotIn("PRIVATE", str(failure.exception))
        incoming = document()
        incoming["treatments"][0]["beats"][0]["name"] = "x" * 65
        with self.assertRaises(ValueError):
            copilot.validate_treatments(shot(), incoming)


if __name__ == "__main__":
    unittest.main()
