import contextlib
import hashlib
import io
import json
import pathlib
import tempfile
import unittest
import zoneinfo
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest import mock

from PIL import Image, ImageDraw, ImageFont, ImageOps
from renderer import app
from tools.release_version import next_version, release_kind
from tools.update_servarr_pin import update_pin


ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"


NOW = datetime(2026, 7, 19, 18, 0, tzinfo=timezone.utc)


class ParserContract(unittest.TestCase):
    def fixture_json(self, name):
        return json.loads((FIXTURES / name).read_text())

    def test_claude_fixture(self):
        result = app.parse_claude_usage(
            self.fixture_json("claude-usage.json"), now=NOW
        )
        self.assertEqual(result["session_pct"], 42)
        self.assertEqual(result["week_pct"], 63)
        self.assertEqual(result["extra_pct"], 12)
        self.assertEqual(result["updated"], NOW.isoformat())

    def test_codex_fixture(self):
        result = app.parse_codex_usage(
            self.fixture_json("codex-usage.json"), now=NOW
        )
        self.assertEqual(
            [(item["label"], item["pct"]) for item in result["windows"]],
            [("5h", 25), ("7d", 50)],
        )
        self.assertEqual(result["credit_balance"], 17)
        self.assertEqual(result["updated"], NOW.isoformat())

    def test_opencode_fixture(self):
        result = app.parse_opencode_usage(
            (FIXTURES / "opencode-usage.html").read_text(), now=NOW
        )
        self.assertEqual(
            (result["fivehr_pct"], result["week_pct"], result["month_pct"]),
            (31, 53, 70),
        )
        self.assertEqual(result["updated"], NOW.isoformat())

    def test_opencode_markup_drift_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "no usage windows found"):
            app.parse_opencode_usage("<html>changed dashboard</html>", now=NOW)

    def test_openrouter_fixture_sums_month_across_keys(self):
        keys = self.fixture_json("openrouter-keys.json")["data"]
        with mock.patch.object(app, "TZINFO", timezone.utc):
            result = app.parse_openrouter_usage(keys, now=NOW)
        # Disabled keys count; BYOK spend does not.
        self.assertEqual(result["month_usd"], 12.4)
        self.assertEqual(result["keys"], 2)
        self.assertEqual(result["month_reset"], "Aug 01, 00:00")
        self.assertEqual(result["updated"], NOW.isoformat())

    def test_openrouter_december_resets_in_january(self):
        with mock.patch.object(app, "TZINFO", timezone.utc):
            result = app.parse_openrouter_usage(
                [], now=datetime(2026, 12, 31, 23, 0, tzinfo=timezone.utc)
            )
        self.assertEqual(result["month_usd"], 0)
        self.assertEqual(result["month_reset"], "Jan 01, 00:00")

    def test_openrouter_fetch_pages_until_no_new_keys(self):
        keys = self.fixture_json("openrouter-keys.json")["data"]
        pages = [{"data": keys[:1]}, {"data": keys[1:]}, {"data": []}]
        with mock.patch.object(app, "requests") as req:
            req.get.return_value.json.side_effect = pages
            result = app.fetch_openrouter_once()
        self.assertEqual(result["month_usd"], 12.4)
        offsets = [c.kwargs["params"]["offset"] for c in req.get.call_args_list]
        self.assertEqual(offsets, [0, 1, 2])
        params = req.get.call_args.kwargs["params"]
        self.assertEqual(
            (params["include_disabled"], params["include_expired"]), ("true", "true")
        )

    def test_openrouter_fetch_stops_when_offset_is_ignored(self):
        keys = self.fixture_json("openrouter-keys.json")["data"]
        with mock.patch.object(app, "requests") as req:
            req.get.return_value.json.return_value = {"data": keys}
            result = app.fetch_openrouter_once()
        self.assertEqual(result["keys"], 2)
        self.assertEqual(req.get.call_count, 2)

    def test_openrouter_unexpected_response_fails_closed(self):
        with mock.patch.object(app, "requests") as req:
            req.get.return_value.json.return_value = {"error": {"code": 403}}
            with self.assertRaisesRegex(RuntimeError, "no key list"):
                app.fetch_openrouter_once()

    def test_open_meteo_fixture(self):
        sao_paulo = zoneinfo.ZoneInfo("America/Sao_Paulo")
        now = datetime(2026, 10, 29, 17, 0, tzinfo=sao_paulo)
        with mock.patch.object(app, "TZINFO", sao_paulo), mock.patch.object(app, "FORECAST_DAYS", 3):
            result = app.parse_open_meteo(self.fixture_json("open-meteo-forecast.json"), now=now)
        self.assertEqual(result["weather"], ("rainy", 18.7, 20.9))
        self.assertEqual(result["forecast"], [
            ("Thu", "rainy", 20.3, 16.3), ("Fri", "rainy", 25.6, 17.0), ("Sat", "sunny", 21.3, 15.7),
        ])
        self.assertEqual([pct for _, pct in result["hourly"]], [24, 30, 43, None])
        self.assertEqual(result["hourly"][0][0], datetime(2026, 10, 29, tzinfo=sao_paulo))
        # After today's sunrise: the next sunrise is tomorrow's, the sunset today's.
        rise, sett = result["sun"]
        self.assertEqual((rise.day, rise.strftime("%H:%M")), (30, "05:21"))
        self.assertEqual((sett.day, sett.strftime("%H:%M")), (29, "18:18"))
        self.assertIsNone(app.parse_open_meteo({}))

    def test_wmo_codes_map_to_ha_conditions(self):
        cases = {
            (0, 1): "sunny", (0, 0): "clear-night", (1, 0): "clear-night",
            (2, 0): "partlycloudy", (3, 1): "cloudy", (48, 1): "fog", (55, 1): "rainy",
            (65, 1): "pouring", (67, 1): "snowy-rainy", (75, 1): "snowy",
            (95, 1): "lightning", (99, 1): "lightning-rainy", (1234, 1): None,
        }
        for (code, is_day), cond in cases.items():
            self.assertEqual(app.wmo_condition(code, is_day), cond, code)

    def test_open_meteo_fetch_uses_the_home_location_and_units(self):
        with (
            mock.patch.object(app, "DASH_LAT", ""),
            mock.patch.object(app, "requests") as req,
        ):
            self.assertIsNone(app.fetch_open_meteo())
        req.get.assert_not_called()
        with (
            mock.patch.object(app, "DASH_LAT", "-23.55"),
            mock.patch.object(app, "DASH_LON", "-46.63"),
            mock.patch.object(app, "DASH_UNITS", "imperial"),
            mock.patch.object(app, "requests") as req,
        ):
            req.get.return_value.json.return_value = self.fixture_json("open-meteo-forecast.json")
            self.assertEqual(app.fetch_open_meteo()["weather"][0], "rainy")
        params = req.get.call_args.kwargs["params"]
        self.assertEqual((params["latitude"], params["longitude"]), ("-23.55", "-46.63"))
        self.assertEqual(params["temperature_unit"], "fahrenheit")


class RenderContract(unittest.TestCase):
    def test_claude_rows_omit_extra_spend(self):
        usage = {
            "session_pct": 3,
            "session_reset": "",
            "week_pct": 4,
            "week_reset": "Jul 25, 08:00",
            "extra_enabled": True,
            "extra_pct": 99,
        }
        with (
            mock.patch.object(app, "read_usage", return_value=(usage, False)),
            mock.patch.object(app, "_usage_block") as block,
        ):
            app.w_usage(mock.Mock(), (0, 0, 300, 300))
        self.assertEqual(
            block.call_args.args[3],
            [
                ("5h", 3, ""),
                ("7d", 4, "Jul 25, 08:00"),
            ],
        )

    def openrouter_rows(self, budget):
        usage = {"month_usd": 12.4, "month_reset": "Aug 01, 00:00"}
        with (
            mock.patch.object(app, "read_usage", return_value=(usage, False)),
            mock.patch.object(app, "OPENROUTER_MONTH_BUDGET", budget),
            mock.patch.object(app, "_usage_block") as block,
        ):
            app.w_openrouter(mock.Mock(), (0, 0, 300, 300))
        return block.call_args.args[3]

    def test_openrouter_row_fills_bar_against_budget(self):
        self.assertEqual(
            self.openrouter_rows(30.0),
            [("Month", None, "$12.40 / $30  ·  Aug 01, 00:00", 41)],
        )

    def test_openrouter_row_without_budget_has_no_fill(self):
        self.assertEqual(
            self.openrouter_rows(None),
            [("Month", None, "$12.40  ·  Aug 01, 00:00", None)],
        )

    def test_bar_fill_override_fills_bar_without_percent_text(self):
        image = Image.new("L", (300, 300), 255)
        draw = ImageDraw.Draw(image)

        with mock.patch.object(app, "_font", return_value=ImageFont.load_default()):
            app._usage_block(
                draw, (0, 0, 300, 300), "OpenRouter", [("Month", None, "$12.40", 50)], False
            )

        self.assertEqual(image.getpixel((102, 70)), 0)

    def test_battery_query_parses_level_and_charging(self):
        self.assertEqual(app.parse_battery({"battery": ["82"], "charging": ["1"]}), (82, True))
        self.assertEqual(app.parse_battery({"battery": ["82"]}), (82, False))
        for q in ({}, {"battery": ["n/a"]}, {"battery": ["150"]}):
            self.assertIsNone(app.parse_battery(q))

    def test_battery_hidden_when_device_does_not_report_it(self):
        image = Image.new("L", (200, 30), 255)
        app.w_battery(ImageDraw.Draw(image), (0, 0, 200, 30), None)
        self.assertEqual(image.getextrema(), (255, 255))

    def test_battery_glyph_fills_with_level(self):
        def inside(pct):
            image = Image.new("L", (200, 30), 255)
            with mock.patch.object(app, "_font", return_value=ImageFont.load_default()):
                app.w_battery(ImageDraw.Draw(image), (0, 0, 200, 30), (pct, False))
            # Left end of the glyph's interior: filled at any non-zero level.
            return image.getpixel((165, 15))

        self.assertEqual(inside(100), 0)
        self.assertEqual(inside(0), 255)

    @unittest.skipUnless(
        pathlib.Path(app.FONT_DIR, "DejaVuSans-Bold.ttf").exists(),
        "needs the DejaVu fonts (DASH_FONT_DIR): the overlap depends on their widths",
    )
    def test_long_temperature_stays_clear_of_the_battery(self):
        def frame(widgets, temp):
            with (
                mock.patch.object(app, "KINDLE_W", 1024),
                mock.patch.object(app, "KINDLE_H", 758),
                mock.patch.object(app, "DASH_WIDGETS", widgets),
                mock.patch.dict(app.DATA, {"weather": ("sunny", temp, temp)}),
            ):
                return Image.open(BytesIO(app.render(rotate=0, battery=(100, True))))

        battery = frame(["battery"], 100)
        # The battery with its bolt (the header row, above the first rule),
        # plus the half-pad gap to its left.
        x0, y0, x1, y1 = ImageOps.invert(battery.crop((0, 0, 1024, 150))).getbbox()
        x0 -= max(16, 1024 // 42) // 2
        # 100° (a °F afternoon) and -10° ran into the bolt before.
        for temp in (100, -10, 21):
            both = frame(["weather", "battery"], temp)
            self.assertNotEqual(both.tobytes(), battery.tobytes())  # the temperature is drawn
            self.assertEqual(
                both.crop((x0, y0, x1, y1)).tobytes(), battery.crop((x0, y0, x1, y1)).tobytes(), temp
            )

    def test_one_percent_usage_has_visible_fill_inside_outline(self):
        image = Image.new("L", (300, 300), 255)
        draw = ImageDraw.Draw(image)

        with mock.patch.object(app, "_font", return_value=ImageFont.load_default()):
            app._usage_block(
                draw, (0, 0, 300, 300), "Claude", [("7d", 1, "")], False
            )

        # Bar starts at x=99 with a 3px outline. A non-zero value must fill
        # at least the first interior pixel instead of disappearing under it.
        self.assertEqual(image.getpixel((102, 70)), 0)

    def test_agenda_merges_calendars(self):
        start = datetime(2026, 7, 19, tzinfo=app.TZINFO)

        def at(hours):
            return {"dateTime": (start + timedelta(hours=hours)).isoformat()}

        personal = [
            {"start": at(15), "summary": "Gym"},
            {"start": {"date": "2026-07-19"}, "summary": "Birthday"},
            # Began yesterday: shows its end time, sorted with the all-day events.
            {"start": at(-2), "end": at(5.5), "summary": "Sleep"},
        ]
        work = [
            {"start": at(9.5), "summary": "Standup"},
            # A free/busy-only share gives no title.
            {"start": at(11), "summary": ""},
            # The same invite on both calendars shows once.
            {"start": at(15), "summary": "Gym"},
        ]
        result = app.parse_events([personal, work], start)
        self.assertEqual([(lbl, s) for _, lbl, s in result], [
            ("all day", "Birthday"),
            ("→05:30", "Sleep"),
            ("09:30", "Standup"),
            ("11:00", "busy"),
            ("15:00", "Gym"),
        ])
        self.assertEqual([at for at, _, _ in result], [
            None,
            start + timedelta(hours=5.5),
            start + timedelta(hours=9.5),
            start + timedelta(hours=11),
            start + timedelta(hours=15),
        ])

    def test_ics_fixture(self):
        sao_paulo = zoneinfo.ZoneInfo("America/Sao_Paulo")
        start = datetime(2026, 10, 29, tzinfo=sao_paulo)
        with mock.patch.object(app, "TZINFO", sao_paulo):
            events = app.parse_ics((FIXTURES / "calendar.ics").read_bytes().decode(), start)
            result = app.parse_events([events], start)
        # A weekly repeat, a moved repeat instance, an all-day event, an
        # overnight event, and a UTC event. Not shown: a cancelled event, an
        # EXDATE instance (yesterday), and tomorrow's event.
        self.assertEqual([(lbl, s) for _, lbl, s in result], [
            ("all day", "Holiday"),
            ("→06:15", "Flight"),
            ("09:30", "Standup"),
            ("12:00", "Call"),
            ("16:00", "Review (moved)"),
        ])

    def test_agenda_merges_ics_and_ha_calendars(self):
        sao_paulo = zoneinfo.ZoneInfo("America/Sao_Paulo")
        now = datetime(2026, 10, 29, 8, 0, tzinfo=sao_paulo)
        ha = [
            # The same invite as the ICS Standup shows once.
            {"start": {"dateTime": "2026-10-29T09:30:00-03:00"}, "summary": "Standup"},
            {"start": {"dateTime": "2026-10-29T18:00:00-03:00"}, "summary": "Gym"},
        ]
        feed = mock.Mock(text=(FIXTURES / "calendar.ics").read_bytes().decode())
        missing = mock.Mock()
        missing.raise_for_status.side_effect = app.requests.HTTPError(
            "404 Client Error: Not Found for url: https://secret.example/b.ics",
            response=mock.Mock(status_code=404),
        )
        log = io.StringIO()
        with (
            mock.patch.object(app, "TZINFO", sao_paulo),
            mock.patch.object(app, "HA_CALENDAR_ENTITIES", ["calendar.work"]),
            mock.patch.object(app, "DASH_ICS_URLS", ["https://secret.example/a.ics",
                                                     "https://secret.example/b.ics"]),
            mock.patch.object(app, "_ha", return_value=ha),
            mock.patch.object(app.requests, "get", side_effect=[feed, missing]),
            contextlib.redirect_stdout(log),
        ):
            result = app.fetch_events(now)
        self.assertEqual([s for _, _, s in result], [
            "Holiday", "Flight", "Standup", "Call", "Review (moved)", "Gym",
        ])
        # A failed feed is named by its number: its URL is a secret.
        self.assertIn("[ics] calendar 2 failed: HTTP 404", log.getvalue())
        self.assertNotIn("secret", log.getvalue())
        # Every feed failed: no calendar, not "nothing scheduled".
        with (
            mock.patch.object(app, "HA_CALENDAR_ENTITIES", []),
            mock.patch.object(app, "DASH_ICS_URLS", ["https://secret.example/a.ics"]),
            mock.patch.object(app.requests, "get", side_effect=app.requests.ConnectionError("x")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertIsNone(app.fetch_events())

    def test_agenda_hides_events_past_the_grace_period(self):
        start = datetime(2026, 7, 19, tzinfo=app.TZINFO)

        def at(hours):
            return start + timedelta(hours=hours)

        events = [
            (None, "all day", "Birthday"),
            (at(5.5), "→05:30", "Sleep"),
            (at(8), "08:00", "Standup"),
            (at(9), "09:00", "Review"),
            (at(10), "10:00", "Lunch"),
        ]
        with mock.patch.object(app, "MAX_EVENTS", 10), mock.patch.object(app, "EVENT_GRACE_MIN", 15):
            # An 08:00 event still shows at 08:15 ...
            self.assertEqual(
                [s for _, s in app.visible_events(events, at(8.25))],
                ["Birthday", "Standup", "Review", "Lunch"],
            )
            # ... and is gone one minute later. All-day events stay.
            self.assertEqual(
                [s for _, s in app.visible_events(events, at(8.25) + timedelta(minutes=1))],
                ["Birthday", "Review", "Lunch"],
            )
        # The row cap applies after past events are dropped.
        with mock.patch.object(app, "MAX_EVENTS", 2):
            self.assertEqual(
                app.visible_events(events, at(9)),
                [("all day", "Birthday"), ("09:00", "Review")],
            )

    def test_agenda_keeps_calendars_that_fetched(self):
        work = [{"start": {"date": "2026-07-19"}, "summary": "Offsite"}]
        with (
            mock.patch.object(app, "HA_CALENDAR_ENTITIES", ["calendar.a", "calendar.b"]),
            mock.patch.object(app, "_ha", side_effect=[None, work]),
        ):
            self.assertEqual(app.fetch_events(), [(None, "all day", "Offsite")])
        with (
            mock.patch.object(app, "HA_CALENDAR_ENTITIES", ["calendar.a", "calendar.b"]),
            mock.patch.object(app, "_ha", return_value=None),
        ):
            self.assertIsNone(app.fetch_events())

    def test_rain_chart_skips_past_hours(self):
        now = datetime.now(app.TZINFO).replace(minute=0, second=0, microsecond=0)
        past = [(now - timedelta(hours=2), 100), (now - timedelta(hours=1), 100)]
        image = Image.new("L", (400, 120), 255)
        draw = ImageDraw.Draw(image)
        with mock.patch.dict(app.DATA, {"hourly": past + [(now, 0)]}):
            app.w_rain(draw, (0, 0, 400, 120))
        # The first column is the current 0% hour, not a past 100% one: the
        # inside of its bar stays white.
        self.assertEqual(image.getpixel((16, 70)), 255)

    def test_empty_fixture_render_is_deterministic_png(self):
        widgets = app.DASH_WIDGETS
        try:
            app.DASH_WIDGETS = []
            first = app.render(rotate=0)
            second = app.render(rotate=0)
        finally:
            app.DASH_WIDGETS = widgets
        self.assertEqual(hashlib.sha256(first).digest(), hashlib.sha256(second).digest())
        image = Image.open(BytesIO(first))
        self.assertEqual(image.format, "PNG")
        self.assertEqual(image.mode, "L")
        self.assertEqual(image.size, (app.KINDLE_W, app.KINDLE_H))


class ReleaseVersionContract(unittest.TestCase):
    TAGS = ["v0.1.0", "not-a-version", "v0.1.1"]

    def test_unlabeled_change_skips_release(self):
        self.assertIsNone(next_version(self.TAGS, []))

    def test_explicit_bumps(self):
        self.assertEqual(next_version(self.TAGS, ["release:patch"]), "v0.1.2")
        self.assertEqual(next_version(self.TAGS, ["release:minor"]), "v0.2.0")
        self.assertEqual(next_version(self.TAGS, ["release:major"]), "v1.0.0")

    def test_none_skips_release(self):
        self.assertIsNone(next_version(self.TAGS, ["release:none"]))

    def test_conflicts_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "conflicting release labels"):
            release_kind(["release:patch", "release:major"])


class PublishWorkflowContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / ".github/workflows/publish.yml").read_text()

    def test_release_is_merge_driven_not_tag_driven(self):
        self.assertIn("pull_request:", self.workflow)
        self.assertIn("types: [closed]", self.workflow)
        self.assertIn("github.event.pull_request.merged == true", self.workflow)
        self.assertNotIn('tags:\n      - "v*"', self.workflow)

    def test_verified_artifacts_precede_git_tag(self):
        sign = self.workflow.index("cosign sign")
        signature = self.workflow.index("cosign verify")
        provenance = self.workflow.index("--format '{{ json .Provenance.SLSA }}'")
        sbom = self.workflow.index("--format '{{ json .SBOM.SPDX }}'")
        provenance_shape = self.workflow.index(
            '.buildDefinition.buildType | type == "string"'
        )
        dependencies_shape = self.workflow.index(
            ".buildDefinition.resolvedDependencies | type == \"array\""
        )
        builder_shape = self.workflow.index(
            '.runDetails.builder.id | type == "string"'
        )
        sbom_shape = self.workflow.index('.SPDXID == "SPDXRef-DOCUMENT"')
        tag_identity = self.workflow.index('git config user.name "github-actions[bot]"')
        tag_email = self.workflow.index(
            'git config user.email "41898282+github-actions[bot]@users.noreply.github.com"'
        )
        tag = self.workflow.index('git tag --annotate "$VERSION"')
        self.assertLess(
            max(
                sign,
                signature,
                sbom,
                provenance,
                provenance_shape,
                dependencies_shape,
                builder_shape,
                sbom_shape,
                tag_identity,
                tag_email,
            ),
            tag,
        )

    def test_keyless_identity_is_narrow(self):
        self.assertIn("id-token: write", self.workflow)
        self.assertIn(
            "https://github.com/ErikBPF/kindle-dash/"
            ".github/workflows/publish.yml@refs/heads/main",
            self.workflow,
        )
        self.assertIn("https://token.actions.githubusercontent.com", self.workflow)

    def test_servarr_checks_are_discovered_before_watch(self):
        discover = self.workflow.index(
            '--repo ErikBPF/servarr "$pr_number" --json name'
        )
        watch = self.workflow.index(
            'gh pr checks --repo ErikBPF/servarr "$pr_number" \\\n'
            "            --watch --interval 10"
        )
        self.assertIn("for attempt in {1..12}; do", self.workflow)
        self.assertIn("No checks registered for Servarr PR", self.workflow)
        self.assertLess(discover, watch)


class SecurityWorkflowContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / ".github/workflows/ci.yml").read_text()

    def test_trivy_scans_built_image_report_only_from_pinned_action(self):
        build = self.workflow.index("docker build --tag kindle-dash:test renderer")
        scan = self.workflow.index("aquasecurity/trivy-action@")
        self.assertLess(build, scan)
        self.assertIn(
            "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25",
            self.workflow,
        )
        self.assertIn("image-ref: kindle-dash:test", self.workflow)
        self.assertIn("exit-code: \"0\"", self.workflow)

class ServarrPinContract(unittest.TestCase):
    def test_updates_exactly_one_immutable_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "kindle-dash.compose.yml"
            path.write_text(
                "services:\n"
                "  kindle-dash:\n"
                "    image: harbor.homelab.pastelariadev.com/library/"
                f"kindle-dash:0.1.1@sha256:{'1' * 64}\n"
            )
            update_pin(path, "v0.3.0", f"sha256:{'2' * 64}")
            self.assertIn(
                f"kindle-dash:v0.3.0@sha256:{'2' * 64}",
                path.read_text(),
            )

    def test_rejects_missing_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "kindle-dash.compose.yml"
            path.write_text("services: {}\n")
            with self.assertRaisesRegex(ValueError, "found 0"):
                update_pin(path, "v0.3.0", f"sha256:{'2' * 64}")


if __name__ == "__main__":
    unittest.main()
