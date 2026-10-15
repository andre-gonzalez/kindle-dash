import hashlib
import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest import mock

from PIL import Image, ImageDraw, ImageFont
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

    def test_hourly_forecast_parses_rain_probability(self):
        resp = {"service_response": {"weather.home": {"forecast": [
            {"datetime": "2026-07-19T18:00:00+00:00", "precipitation_probability": 40},
            {"datetime": "2026-07-19T19:00:00+00:00", "precipitation_probability": 72.6},
            {"datetime": "2026-07-19T20:00:00+00:00"},
            {"precipitation_probability": 10},
        ]}}}
        result = app.parse_hourly(resp, "weather.home")
        self.assertEqual([pct for _, pct in result], [40, 73, None])
        self.assertEqual(result[0][0], NOW)
        self.assertIsNone(app.parse_hourly(None, "weather.home"))
        self.assertIsNone(app.parse_hourly(resp, "weather.other"))

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
        with mock.patch.object(app, "MAX_EVENTS", 10):
            result = app.parse_events([personal, work], start)
        self.assertEqual(result, [
            ("all day", "Birthday"),
            ("→05:30", "Sleep"),
            ("09:30", "Standup"),
            ("11:00", "busy"),
            ("15:00", "Gym"),
        ])

    def test_agenda_keeps_calendars_that_fetched(self):
        work = [{"start": {"date": "2026-07-19"}, "summary": "Offsite"}]
        with (
            mock.patch.object(app, "HA_CALENDAR_ENTITIES", ["calendar.a", "calendar.b"]),
            mock.patch.object(app, "_ha", side_effect=[None, work]),
        ):
            self.assertEqual(app.fetch_events(), [("all day", "Offsite")])
        with (
            mock.patch.object(app, "HA_CALENDAR_ENTITIES", ["calendar.a", "calendar.b"]),
            mock.patch.object(app, "_ha", return_value=None),
        ):
            self.assertIsNone(app.fetch_events())

    def test_open_meteo_parses_rain_probability(self):
        data = {"hourly": {
            "time": [int(NOW.timestamp()), int(NOW.timestamp()) + 3600],
            "precipitation_probability": [41, None],
        }}
        result = app.parse_open_meteo(data)
        self.assertEqual([pct for _, pct in result], [41, None])
        self.assertEqual(result[0][0], NOW)
        self.assertIsNone(app.parse_open_meteo({}))

    def test_hourly_falls_back_to_open_meteo_without_probability(self):
        # Met.no-style hourly forecast: rain amount, no probability.
        ha_hourly = {"service_response": {"weather.home": {"forecast": [
            {"datetime": "2026-07-19T18:00:00+00:00", "precipitation": 0.4},
        ]}}}
        meteo = [(NOW, 55)]

        def fake_ha(method, path, body=None, **params):
            return ha_hourly if path.endswith("get_forecasts") else {"latitude": 1, "longitude": 2}

        with (
            mock.patch.object(app, "HA_WEATHER_ENTITY", "weather.home"),
            mock.patch.object(app, "_ha", side_effect=fake_ha),
            mock.patch.object(app, "requests") as req,
            mock.patch.object(app, "parse_open_meteo", return_value=meteo),
        ):
            self.assertEqual(app.fetch_hourly(), meteo)
        params = req.get.call_args.kwargs["params"]
        self.assertEqual((params["latitude"], params["longitude"]), (1, 2))

    def test_rain_chart_skips_past_hours(self):
        now = datetime.now(app.TZINFO).replace(minute=0, second=0, microsecond=0)
        past = [(now - timedelta(hours=2), 100), (now - timedelta(hours=1), 100)]
        image = Image.new("L", (400, 120), 255)
        draw = ImageDraw.Draw(image)
        with mock.patch.dict(app.HA_DATA, {"hourly": past + [(now, 0)]}):
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
