"""Static checks on the served pages.

Three real defects motivated these, and none of them raised anything at the
time:

* The full-screen dock shipped with complete markup and complete behaviour and
  *no stylesheet at all*. `.fsdock`, `.fscollapse`, `.fshud` and `.collapsed`
  had zero rules, so the overlay rendered as ordinary flow content. It looked
  like the feature had been ignored rather than half-built.
* A handler referenced `clutchHeld`, which never existed. It threw during page
  load, which in a single inline script takes every later handler down with it.
* A block of markup failed to apply during an edit, leaving the script looking
  up four ids that were not in the document.

All three are invisible to Python tests and to anything that only parses the
page. The checks run over every page the server serves, because the second one
inherits these failure modes the moment it exists.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


class Page:
    """One served page, split into the parts each check needs."""

    def __init__(self, name: str, extra_script: str = ""):
        self.name = name
        self.html = (WEB / name).read_text(encoding="utf-8")
        self.script = "\n".join(
            re.findall(r"<script[^>]*>(.*?)</script>", self.html, re.S))
        if extra_script:
            self.script += "\n" + (WEB / extra_script).read_text(encoding="utf-8")
        self.style = "\n".join(
            re.findall(r"<style[^>]*>(.*?)</style>", self.html, re.S))

    @property
    def markup_classes(self) -> set[str]:
        out: set[str] = set()
        for attr in re.findall(r'class="([^"]*)"', self.html):
            # Rows are built from template literals, so a class attribute can
            # carry an interpolation: class="flow-tag ${w.flow ? "on" : ""}".
            # The expression is not a class name and cannot be checked
            # statically -- the literals it can produce are covered by DYNAMIC.
            # Left in, it reported '${w.flow' as an unstyled class.
            attr = re.sub(r"\$\{[^}]*(\}|$)", " ", attr)
            out.update(c for c in attr.split() if c)
        return out

    @property
    def styled_classes(self) -> set[str]:
        return set(re.findall(r"\.([A-Za-z][\w-]*)", self.style))

    @property
    def ids(self) -> set[str]:
        return set(re.findall(r'\bid="([^"]+)"', self.html))

    @property
    def looked_up_ids(self) -> set[str]:
        used = set(re.findall(r'\$\("([^"]+)"\)', self.script))
        used |= set(re.findall(r'getElementById\("([^"]+)"\)', self.script))
        return used


# Classes applied only from JavaScript, or supplied by the browser. A class
# here is exempt from needing a rule; keep the list short and justified.
DYNAMIC = {
    "hidden",                                   # generic show/hide, has a rule
    "on", "off", "held", "collapsed",
    "tl", "tr", "bl", "br",                     # corner docks
    "circled", "clean", "bad", "connected", "connecting", "error",
    "rec", "confirmed", "peek", "sun",          # body state on the monitor
    "poor", "low", "armed", "nodata", "soon", "open",
}

PAGES = [Page("index.html"), Page("cine.html", extra_script="monitor.js"),
        Page("mobile.html")]

# An attribute whose value contains a ${...} interpolation carrying a double
# quote. The quote closes the attribute early, so the browser reads the rest as
# stray attributes and the class guard above sees fragments of JavaScript.
ATTR_WITH_INNER_QUOTE = re.compile(
    r'[\w-]+="[^"\n]*\$\{[^}\n]*"'
)


class TestEveryClassIsStyled(unittest.TestCase):
    def test_markup_classes_have_a_rule(self):
        """A class in the markup with no rule anywhere is a half-shipped
        feature -- exactly how the full-screen dock went out."""
        for page in PAGES:
            with self.subTest(page=page.name):
                missing = sorted(page.markup_classes - page.styled_classes - DYNAMIC)
                self.assertEqual(missing, [],
                                 f"{page.name}: used but never styled: {missing}")


class TestNothingIsDefinedTwice(unittest.TestCase):
    """The motion-timelapse block and the move timeline both used `.tl`;
    the later rule's 34px overflow:hidden clipped the whole timelapse section,
    and two elements shared id="tlFill" so timelapse progress drove the shot
    timeline. Every class had a rule and every id existed, so nothing here
    noticed."""

    def test_no_id_is_used_twice(self):
        for page in PAGES:
            with self.subTest(page=page.name):
                ids = re.findall(r'\bid="([^"$]+)"', page.html)
                dup = sorted({i for i in ids if ids.count(i) > 1})
                self.assertEqual(dup, [], f"{page.name}: duplicate ids: {dup}")

    def test_the_timelapse_block_is_not_the_move_timeline(self):
        # Both classes stay styled, so the two checks above cannot see the
        # markup side of the same clash: a timelapse wrapper on `.tl` picks
        # up the 34px overflow:hidden timeline rule and clips its children.
        page = Page("index.html")
        self.assertNotRegex(page.html, r'class="tl"[^>]*>\s*<div class="tl-head"')
        self.assertRegex(page.html, r'class="lapse"[^>]*>\s*<div class="tl-head"')

    def test_no_bare_class_selector_is_declared_twice(self):
        # A repeated bare `.name{` at equal specificity means the later block
        # silently wins for every property it sets.
        for page in PAGES:
            with self.subTest(page=page.name):
                # A responsive override inside @media is the one legitimate
                # repeat; the clash that clipped the timelapse was at top level.
                base = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*\s*\}", "",
                              page.style, flags=re.S)
                names = re.findall(r"(?m)^\s*\.([\w-]+)\s*\{", base)
                dup = sorted({n for n in names if names.count(n) > 1})
                self.assertEqual(dup, [], f"{page.name}: class declared twice: {dup}")


class TestRollExists(unittest.TestCase):
    def test_the_panel_can_roll(self):
        page = Page("index.html")
        self.assertIn('id="btnRoll"', page.html)
        self.assertIn('post("/api/move/roll"', page.script)
        self.assertIn('$("btnRoll").disabled = $("btnPlay").disabled;', page.script)


class TestScriptReferences(unittest.TestCase):
    # The status strip is built in a loop that names each wrapper
    # `<field id>_w`, so those ids exist at runtime but never appear literally
    # in the markup for this check to find. Matched by shape rather than
    # listed one by one, because the point of the loop is that fields get
    # added.
    BUILT_AT_RUNTIME = re.compile(r"^f[A-Z]\w*_w$")

    def test_every_element_looked_up_by_id_exists(self):
        for page in PAGES:
            with self.subTest(page=page.name):
                missing = sorted(
                    i for i in page.looked_up_ids - page.ids
                    if not self.BUILT_AT_RUNTIME.match(i))
                self.assertEqual(missing, [],
                                 f"{page.name}: looks up missing ids: {missing}")

    def test_a_runtime_built_id_still_has_a_field_that_makes_it(self):
        """The exemption above must not become a hole: every `fX_w` looked up
        needs a matching `{id:"fX"` in the field table that builds it."""
        page = Page("cine.html")
        for wid in sorted(i for i in page.looked_up_ids
                          if self.BUILT_AT_RUNTIME.match(i)):
            with self.subTest(id=wid):
                # assertTrue, not assertIn: assertIn prints the haystack,
                # and the haystack here is the whole page.
                self.assertTrue(f'id:"{wid[:-2]}"' in page.html,
                                f"{wid} is looked up but no field declares it")


class TestScriptParses(unittest.TestCase):
    def test_node_accepts_every_page(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        for page in PAGES:
            with self.subTest(page=page.name):
                fd, path = tempfile.mkstemp(suffix=".js")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(page.script)
                    done = subprocess.run([node, "--check", path],
                                          capture_output=True, text=True)
                    self.assertEqual(done.returncode, 0, done.stderr)
                finally:
                    os.unlink(path)


class TestEngineeringPanel(unittest.TestCase):
    page = PAGES[0]

    def test_every_gauge_and_sector_direction_is_present(self):
        """The four limit gauges are the whole point of the per-axis rework."""
        for want in ("gaugeUp", "gaugeDown", "gaugeLeft", "gaugeRight"):
            self.assertIn(f'id="{want}"', self.page.html)
        for want in ("up", "down", "left", "right"):
            self.assertIn(f'data-nudge="{want}"', self.page.html)

    def test_the_whole_circle_limit_ring_is_gone(self):
        """It lit the entire control from one number for the entire rig, so a
        pitch limit coloured the pan sectors too."""
        self.assertNotIn("limitRing", self.page.html)
        self.assertNotIn("limitring", self.page.style)


class TestMonitorHonesty(unittest.TestCase):
    """The monitor must not present belief as measurement.

    Every field on it is either something the camera told us or something this
    panel asked for, and those are not the same claim. A monitor that blurs
    them is how a take gets shot on the wrong exposure, or not shot at all.
    """

    page = PAGES[1]

    def test_the_tally_distinguishes_acknowledged_from_confirmed(self):
        """The record opcode is unverified and nothing reports the tally back,
        so an unqualified red border would assert something we cannot know."""
        self.assertIn("REC ACK", self.page.script)
        self.assertIn("body.rec.confirmed", self.page.style)
        self.assertRegex(self.page.style,
                         r"body\.rec #tally\{[^}]*border-style:dashed")

    def test_no_frame_accurate_timecode_is_claimed(self):
        """HH:MM:SS:FF asserts a frame clock and a timecode source, and this
        build has neither."""
        self.assertIn("elapsed", self.page.html.lower())
        self.assertNotIn("tcFrames", self.page.script)

    def test_the_clip_strip_says_it_measures_the_preview(self):
        """It counts a downscaled JPEG, which averages isolated clipped pixels
        away and invents others at block edges."""
        self.assertIn("PREVIEW", self.page.html)

    def test_unavailable_tools_are_marked_rather_than_dead(self):
        """A button that silently does nothing is worse than one that says why."""
        self.assertIn('kind:"soon"', self.page.script)
        self.assertIn(".tool.soon", self.page.style)

    def test_the_stop_control_survives_the_cleanest_density(self):
        """On a touch-only device, hiding the way to stop a take is
        unshippable. Everything else on the page is negotiable."""
        clean = re.findall(r"body\.d-clean[^{]*\{[^}]*\}", self.page.style)
        hidden = "\n".join(r for r in clean if "display:none" in r)
        for keep in ("#btnRec", "#tally", "#hud "):
            self.assertNotIn(keep.strip(), hidden,
                             f"{keep} must never be hidden by DISP")


class TestSharedMonitorModule(unittest.TestCase):
    """Assists live in one file so two copies of the exposure maths cannot
    drift apart, with the wrong one being whichever you are not looking at."""

    def test_the_monitor_page_loads_the_shared_module(self):
        self.assertIn('src="/monitor.js"', PAGES[1].html)

    def test_the_module_exposes_what_the_page_calls(self):
        src = (WEB / "monitor.js").read_text(encoding="utf-8")
        page = PAGES[1].script
        for fn in re.findall(r"Monitor\.(\w+)\(", page):
            with self.subTest(fn=fn):
                self.assertRegex(src, rf"\b{fn}\b\s*[,:(]",
                                 f"page calls Monitor.{fn} which is not defined")


class TestKeyframeAuthoringControls(unittest.TestCase):
    """The sampler grew pass-through nodes and a zoom track. If the editor
    does not expose them the feature is unreachable, which is how the
    full-screen dock shipped with markup and no stylesheet."""

    def setUp(self):
        self.page = Page("index.html")

    def test_the_flow_toggle_is_present_and_wired(self):
        self.assertIn("data-flow", self.page.html)
        self.assertIn("flow-tag", self.page.styled_classes)

    def test_the_flow_state_is_pushed_to_the_host(self):
        """A toggle that only changes a colour is worse than no toggle.

        Anchored on the handler's own querySelectorAll rather than on the
        first `data-flow` in the file -- that one is in the row template, a
        long way above the code that acts on it.
        """
        idx = self.page.html.index('querySelectorAll("[data-flow]")')
        handler = self.page.html[idx:idx + 400]
        self.assertIn("w.flow = !w.flow", handler)
        self.assertIn("pushMove()", handler)

    def test_zoom_and_its_own_easing_are_both_editable(self):
        self.assertIn('data-k="zoom"', self.page.html)
        self.assertIn('data-k="zoom_easing"', self.page.html)

    def test_a_blank_zoom_is_sent_as_null_not_zero(self):
        """Zero is fully wide. Reading a blank field as zero would rack the
        lens out between two framings captured to match."""
        self.assertIn("w.zoom = inp.value.trim() === \"\" ? null", self.page.html)

    def test_capture_can_mark_the_node_as_flow(self):
        self.assertIn("capFlow", self.page.html)
        self.assertIn("flow: $(\"capFlow\").checked", self.page.html)

    def test_the_timeline_distinguishes_a_pass_through(self):
        """A stop is a mark to hit; a flow node is not. They cannot look the
        same on the timeline or the operator cannot read the move."""
        self.assertIn("mk.flow", self.page.style)


class TestAttributesAreWellFormed(unittest.TestCase):
    def test_no_interpolation_puts_a_double_quote_inside_an_attribute(self):
        """`class="a ${x ? "on" : ""}"` closes the attribute at the first inner
        quote. The browser then reads the rest as stray attributes, and the
        class guard above reported '${x' as an unstyled class. Single quotes
        inside the interpolation. Caught twice in one sitting: once in a
        class attribute, once in a value.
        """
        for page in PAGES:
            with self.subTest(page=page.name):
                pattern = ATTR_WITH_INNER_QUOTE
                bad = pattern.findall(page.html)
                self.assertEqual(bad, [], f"{page.name}: {bad}")


class TestMonitorLut(unittest.TestCase):
    """A viewing look changes the picture and not the recording. The monitor
    has to keep saying so, and the exposure tools must not read through it."""

    def setUp(self):
        self.page = Page("cine.html")

    def test_the_lut_tool_is_no_longer_a_placeholder(self):
        self.assertNotIn('{id:"lut",   label:"LUT",    grp:"look",     kind:"soon"}',
                         self.page.html)
        self.assertIn('kind:"look"', self.page.html)

    def test_the_monitor_only_label_is_present(self):
        """An operator who thinks the look is baked in lights for the wrong
        picture."""
        self.assertIn("MONITOR ONLY", self.page.html)
        self.assertIn("lutOnly", self.page.styled_classes | set(
            re.findall(r'id="([\w-]+)"', self.page.html)))

    def test_assists_still_sample_the_raw_feed(self):
        """False colour read off a graded picture reports the look's exposure,
        not the recording's -- the one number that has to be trustworthy."""
        self.assertIn("Monitor.sample(feed)", self.page.html)

    def test_the_look_is_parsed_on_the_host_not_in_the_browser(self):
        self.assertIn('fetch("/api/lut"', self.page.html)
        self.assertNotIn("LUT_3D_SIZE", self.page.html)

    def test_mix_and_exposure_are_both_reachable(self):
        for el in ("lutAmt", "lutExp", "lutLoad", "lutClear"):
            with self.subTest(el=el):
                self.assertIn(f'id="{el}"', self.page.html)

    def test_webgl_absence_is_reported_not_swallowed(self):
        """A look that silently fails to apply is worse than no look."""
        self.assertIn("lutView.supported", self.page.html)
        self.assertIn("needs WebGL2", self.page.html)


class TestShotLibraryUi(unittest.TestCase):
    def setUp(self):
        self.page = Page("index.html")

    def test_the_picker_exists_and_is_wired(self):
        for el in ("libList", "btnLibSave", "btnLibRefresh"):
            with self.subTest(el=el):
                self.assertIn(f'id="{el}"', self.page.html)
        self.assertIn('fetch("/api/moves")', self.page.html)

    def test_a_move_name_is_never_written_as_markup(self):
        """The name comes from a text box and lands in a list row. Written
        with innerHTML, a move called `<img onerror=...>` runs every time the
        library is drawn.
        """
        self.assertIn('.querySelector(".n").textContent = e.name', self.page.html)
        self.assertNotIn("${e.name}", self.page.html)

    def test_the_rigging_note_is_not_written_as_markup_either(self):
        self.assertNotIn("${e.setup}", self.page.html)

    def test_deleting_a_shot_asks_first(self):
        """Small x next to Load, and the action is not undoable."""
        idx = self.page.html.index("async function libDelete")
        self.assertIn("confirm(", self.page.html[idx:idx + 400])

    def test_file_export_survives_alongside_the_library(self):
        """The library is host-side; the file buttons are how a move leaves
        this machine."""
        self.assertIn('$("btnSave").onclick', self.page.html)
        self.assertIn('$("btnLoad").onclick', self.page.html)


class TestPreflightUi(unittest.TestCase):
    def setUp(self):
        self.page = Page("index.html")

    def test_the_button_and_panel_exist(self):
        for el in ("btnPreflight", "pf", "pfSum", "pfList"):
            with self.subTest(el=el):
                self.assertIn(f'id="{el}"', self.page.html)

    def test_it_pushes_the_move_before_judging_it(self):
        """Judging a stale copy would clear a move the host does not hold."""
        idx = self.page.html.index("async function runPreflight")
        head = self.page.html[idx:idx + 300]
        self.assertIn("await pushMove()", head)

    def test_findings_are_written_as_text_not_markup(self):
        self.assertIn("el.textContent = f.detail", self.page.html)

    def test_it_says_the_move_still_runs(self):
        """The runner clamps rather than refusing, and an operator who thinks
        a flagged move will not play will stop trusting the check."""
        self.assertIn("will still run", self.page.html)

    def test_it_names_the_sensitivity_it_judged_against(self):
        self.assertIn("r.speed_preset", self.page.html)


class TestFramingTools(unittest.TestCase):
    """Delivery ratio, safe areas and anamorphic desqueeze."""

    def setUp(self):
        self.page = Page("cine.html")

    def test_the_frame_tool_and_its_controls_exist(self):
        for el in ("framebar", "frameRatio", "frameMask", "frameSqueeze"):
            with self.subTest(el=el):
                self.assertIn(f'id="{el}"', self.page.html)

    def test_the_mask_divides_the_target_by_the_squeeze(self):
        """The guides canvas is a child of #pic and carries the desqueeze
        transform, so a rectangle drawn at aspect R appears at R * k. Drawing
        the raw target instead would put the mask out by exactly the squeeze
        factor -- which looks plausible and frames the wrong shot.
        """
        self.assertIn("const drawAspect = target / squeezeFactor();",
                      self.page.html)

    def test_desqueeze_and_mirror_share_one_transform(self):
        """Two writers of style.transform means whichever ran last wins and
        the other silently stops working."""
        self.assertIn("function applySqueeze", self.page.html)
        self.assertNotIn('$("pic").style.transform = on.mirror ? "scaleX(-1)" : ""',
                         self.page.html)

    def test_desqueeze_scales_y_down_rather_than_x_up(self):
        """Scaling X would push the picture past the edges of the stage. The
        ratio between the axes is what matters, so squashing Y gives the same
        look and is guaranteed to fit."""
        self.assertIn("scaleY(${(1 / k).toFixed(4)})", self.page.html)

    def test_the_squeeze_is_labelled_preview_only(self):
        """It changes the monitor, not the recording."""
        self.assertIn("preview only", self.page.html)

    def test_safe_areas_are_measured_inside_the_delivered_frame(self):
        """Ninety and eighty percent of the DELIVERY rectangle, not of the
        sensor -- a caption sits inside the frame that ships."""
        idx = self.page.html.index("const drawAspect")
        block = self.page.html[idx:idx + 1800]
        self.assertIn("mw * .05", block)
        self.assertIn("mw * .10", block)


class TestPortraitLayout(unittest.TestCase):
    def setUp(self):
        self.page = Page("cine.html")

    def test_there_is_a_portrait_rule(self):
        self.assertIn("@media (orientation:portrait)", self.page.style)

    def test_it_keys_on_orientation_not_width(self):
        """A narrow landscape window is still landscape, and giving it the
        portrait layout wastes the width it does have."""
        idx = self.page.style.index("@media (orientation:portrait)")
        self.assertNotIn("max-width", self.page.style[idx:idx + 120])

    def test_the_tally_column_is_moved_not_hidden(self):
        """An operator who loses the tally by turning the phone has lost the
        one thing that must never be ambiguous."""
        idx = self.page.style.index("@media (orientation:portrait)")
        block = self.page.style[idx:idx + 2000]
        self.assertIn("#leftcol", block)
        self.assertNotIn("#leftcol{ display:none", block)

    def test_the_floating_bars_do_not_stack_on_each_other(self):
        idx = self.page.style.index("@media (orientation:portrait)")
        block = self.page.style[idx:idx + 2000]
        self.assertIn("#framebar{ bottom:196px; }", block)
        self.assertIn("#lutbar{ bottom:150px; }", block)


class TestTravelMap(unittest.TestCase):
    def setUp(self):
        self.page = Page("index.html")

    def test_the_map_exists(self):
        self.assertIn('id="pathMap"', self.page.html)
        self.assertIn("function pathMapDraw", self.page.html)

    def test_there_is_no_second_sampler_in_the_browser(self):
        """The first version sampled the move again in JavaScript. Easing,
        dwell handling and arc routing existed twice, and the day they drift
        the map draws a move that will not be played."""
        for ghost in ("function sampleMoveAt", "function arcDelta", "const EASE"):
            with self.subTest(ghost=ghost):
                self.assertNotIn(ghost, self.page.html)

    def test_it_draws_the_hosts_own_samples(self):
        self.assertIn('fetch("/api/move/path")', self.page.html)

    def test_the_geometry_comes_from_the_host_too(self):
        """Carrying a copy of the calibration in the page means the map can
        be drawing last week's arcs."""
        self.assertIn("if (s.travel) travelGeom = s.travel;", self.page.html)

    def test_a_failed_fetch_draws_no_path_rather_than_a_wrong_one(self):
        idx = self.page.html.index("async function refreshPath")
        self.assertIn("pathPoints = [];", self.page.html[idx:idx + 600])

    def test_the_unreachable_wedge_is_drawn(self):
        """The whole point of the map: 93 degrees of yaw that cannot be
        reached, which had no representation anywhere before."""
        self.assertIn("#3a2020", self.page.html)


class TestStopKeyAndExport(unittest.TestCase):
    def setUp(self):
        self.page = Page("index.html")

    def test_escape_stops_the_rig(self):
        """When something is going wrong the operator's hand is not on the
        mouse hunting for a button."""
        self.assertIn('ev.key !== "Escape"', self.page.html)
        self.assertIn('post("/api/stop")', self.page.html)

    def test_escape_also_stops_a_timelapse(self):
        idx = self.page.html.index('ev.key !== "Escape"')
        self.assertIn('/api/timelapse/stop', self.page.html[idx:idx + 900])

    def test_no_key_starts_motion(self):
        """A space bar that plays a move fires whenever the page has focus,
        which on a rig that swings a camera is a hazard, not a shortcut."""
        idx = self.page.html.index('ev.key !== "Escape"')
        block = self.page.html[idx:self.page.html.index("});", idx)]
        for danger in ('"/api/move/play"', '"/api/goto"', '"/api/timelapse/start"'):
            with self.subTest(route=danger):
                self.assertNotIn(danger, block)

    def test_the_listener_is_registered_once_at_top_level(self):
        """It was first inserted INSIDE the flow-toggle handler, where it
        parses cleanly and re-registers on every toggle -- so one Escape
        would have fired a stop for every time anyone had touched a FLOW
        button. Column zero is the check: a nested handler is indented.
        """
        found = 0
        for n, line in enumerate(self.page.html.splitlines(), start=1):
            if 'addEventListener("keydown"' in line:
                found += 1
                # Every one of them, not the first: an earlier top-level
                # listener would otherwise vouch for a later nested one.
                self.assertFalse(line.startswith((" ", "	")),
                                 f"line {n}: keydown listener is nested inside "
                                 "another function and will be re-registered")
        self.assertGreater(found, 0, "no keydown listener found")

    def test_the_export_handler_is_bound_once_too(self):
        for line in self.page.html.splitlines():
            if '$("btnExportData").onclick' in line:
                self.assertFalse(line.startswith((" ", "	")),
                                 "the export handler is nested and will be "
                                 "rebound on every render")
                return
        self.fail("no export handler found")

    def test_escape_in_a_text_field_cancels_typing_first(self):
        """Taking Escape away from an input is its own small trap. A second
        press still reaches the rig."""
        idx = self.page.html.index('ev.key !== "Escape"')
        self.assertIn("INPUT|SELECT|TEXTAREA", self.page.html[idx:idx + 900])

    def test_the_data_export_carries_the_traces(self):
        """The measured traces are camera tracking data; a CSV cell is the
        wrong place for a time series."""
        self.assertIn('id="btnExportData"', self.page.html)
        idx = self.page.html.index('$("btnExportData")')
        block = self.page.html[idx:idx + 1200]
        self.assertIn("takes:", block)
        self.assertIn("units:", block)

    def test_the_export_states_its_units(self):
        """A consumer needs to know these are gimbal-frame degrees, not a
        world frame."""
        self.assertIn("gimbal frame", self.page.html)


class TestTimelapseOnTheMonitor(unittest.TestCase):
    """The monitor is what is actually on set. A shoot running for hours with
    nobody at the browser has to be legible from there."""

    def setUp(self):
        self.page = Page("cine.html")

    def test_the_band_exists_and_is_wired(self):
        for el in ("tlband", "tlbCount", "tlbFill", "tlbLeft", "tlbStop"):
            with self.subTest(el=el):
                self.assertIn(f'id="{el}"', self.page.html)
        self.assertIn("tlBand(s.timelapse)", self.page.html)

    def test_it_is_hidden_when_nothing_is_running(self):
        """Permanent chrome for an occasional mode spends pixels the picture
        needs."""
        self.assertIn("#tlband{", self.page.style)
        idx = self.page.style.index("#tlband{")
        self.assertIn("display:none", self.page.style[idx:idx + 260])

    def test_the_stop_button_is_the_record_colour(self):
        """The one control on this band is a stop, and stop is the only thing
        in this UI allowed to be red besides the tally."""
        idx = self.page.style.index("#tlband button{")
        self.assertIn("var(--rec)", self.page.style[idx:idx + 200])

    def test_time_left_is_measured_before_it_is_estimated(self):
        """The plan was made before anything moved. Once frames are going
        down, the rig's own pace is the better predictor."""
        idx = self.page.html.index("function tlBand")
        block = self.page.html[idx:idx + 1600]
        self.assertIn("secs / done", block)

    def test_an_estimate_is_labelled_as_one(self):
        self.assertIn("(est)", self.page.html)


class TestBetweenTakesUi(unittest.TestCase):
    def setUp(self):
        self.page = Page("index.html")

    def test_the_loop_is_one_click_each(self):
        for el in ("btnBackToOne", "btnReference", "btnRetimeTo", "btnTrim",
                   "btnExportPath", "startChk"):
            with self.subTest(el=el):
                self.assertIn(f'id="{el}"', self.page.html)

    def test_common_retimes_are_preset_buttons(self):
        """'Same move, slower' arrives constantly; typing a number for it is
        friction in the middle of a take loop."""
        for factor in ("0.5", "1.5", "2"):
            with self.subTest(factor=factor):
                self.assertIn(f'data-retime="{factor}"', self.page.html)

    def test_the_start_check_is_shown_continuously(self):
        """An operator who can see they are off the mark fixes it before the
        director calls roll, not after."""
        self.assertIn("showStartCheck(s.start_check)", self.page.html)

    def test_off_start_is_amber_not_red(self):
        """Nothing is broken; the rig is just somewhere else."""
        idx = self.page.style.index(".startchk.off")
        self.assertIn("#ffb000", self.page.style[idx:idx + 120])

    def test_the_editor_refreshes_after_a_server_side_edit(self):
        """Retime and trim happen on the host. Without a re-read the panel
        keeps drawing the move it had before, and the next push overwrites
        the change."""
        for handler in ("btnReference", "btnRetimeTo", "btnTrim"):
            idx = self.page.html.index(f'$("{handler}").onclick')
            self.assertIn("afterMoveEdit()", self.page.html[idx:idx + 420],
                          f"{handler} does not re-read the move")

    def test_the_export_is_labelled_as_the_measured_path(self):
        self.assertIn("measured path", self.page.html.lower())


class TestExtendedTakeTools(unittest.TestCase):
    def setUp(self):
        self.page = Page("index.html")

    def test_the_lens_can_be_stated_in_the_setup(self):
        """Every pixel figure depends on it and nothing reports it."""
        self.assertIn('id="suFov"', self.page.html)
        self.assertIn('suFov:"fov_deg"', self.page.html)

    def test_a_blank_lens_is_labelled_assumed_in_the_placeholder(self):
        idx = self.page.html.index('id="suFov"')
        self.assertIn("assumed", self.page.html[idx:idx + 120])

    def test_a_node_can_be_rehearsed_from(self):
        self.assertIn("data-seg", self.page.html)
        self.assertIn('post("/api/move/segment"', self.page.html)

    def test_the_last_node_has_no_run_from_here(self):
        """There is nothing after it to run to."""
        idx = self.page.html.index("data-seg")
        self.assertIn("i < last", self.page.html[idx - 200:idx])

    def test_an_interrupted_shoot_is_offered_not_resumed(self):
        """Restarting into a shoot whose scene was struck two hours ago is
        worse than losing it."""
        self.assertIn('fetch("/api/timelapse/resume")', self.page.html)
        idx = self.page.html.index("async function checkResume")
        block = self.page.html[idx:idx + 1800]
        self.assertIn("go.onclick", block)
        self.assertNotIn("autoResume", block)

    def test_an_edited_move_offers_no_resume_button(self):
        idx = self.page.html.index("if (!r.matches)")
        block = self.page.html[idx:idx + 420]
        self.assertIn("return;", block)

    def test_a_stale_resume_says_so(self):
        self.assertIn("twelve hours", self.page.html)


if __name__ == "__main__":
    unittest.main()
