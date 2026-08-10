import unittest

from mock_site.app import create_app


class MockSiteTestCase(unittest.TestCase):
    def setUp(self):
        app = create_app({"TESTING": True, "MOCK_SITE_CONTROLS_ENABLED": True})
        self.client = app.test_client()

    def test_health_endpoint(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_reservation_page_has_stable_machine_readable_markup(self):
        response = self.client.get("/reservations/2026-08-22")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('data-opportunity-id="2026-08-22-morning-pass"', html)
        self.assertIn('data-status="available"', html)
        self.assertIn('data-status="sold-out"', html)
        self.assertIn('datetime="2026-08-22T09:00:00"', html)

    def test_invalid_reservation_date_is_not_found(self):
        self.assertEqual(self.client.get("/reservations/not-a-date").status_code, 404)

    def test_control_can_make_opportunity_available(self):
        response = self.client.patch(
            "/__control/opportunities/midday-pass",
            json={"availability": "available", "capacity": 1},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["availability"], "available")
        page = self.client.get("/reservations/2026-08-22").get_data(as_text=True)
        self.assertIn('data-opportunity-id="2026-08-22-midday-pass"', page)
        self.assertIn("1 space available", page)

    def test_reset_restores_default_state_and_behavior(self):
        self.client.patch(
            "/__control/opportunities/midday-pass",
            json={"availability": "available", "capacity": 1},
        )
        self.client.patch("/__control/behavior", json={"malformed": True})

        response = self.client.post("/__control/reset")

        self.assertEqual(response.status_code, 200)
        state = response.get_json()
        midday = next(
            item for item in state["opportunities"] if item["key"] == "midday-pass"
        )
        self.assertEqual(midday["availability"], "sold-out")
        self.assertEqual(midday["capacity"], 0)
        self.assertFalse(state["behavior"]["malformed"])

    def test_behavior_can_simulate_malformed_page(self):
        self.client.patch("/__control/behavior", json={"malformed": True})
        response = self.client.get("/reservations/2026-08-22")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn("data-opportunity-id", html)
        self.assertIn("intentionally absent", html)

    def test_behavior_can_simulate_server_error(self):
        self.client.patch("/__control/behavior", json={"status_code": 503})
        self.assertEqual(self.client.get("/reservations/2026-08-22").status_code, 503)

    def test_control_validates_updates(self):
        response = self.client.patch(
            "/__control/opportunities/midday-pass",
            json={"availability": "sometimes"},
        )
        self.assertEqual(response.status_code, 400)

    def test_control_can_hide_an_opportunity(self):
        self.client.patch(
            "/__control/opportunities/afternoon-pass", json={"enabled": False}
        )
        page = self.client.get("/reservations/2026-08-22").get_data(as_text=True)
        self.assertNotIn("2026-08-22-afternoon-pass", page)


class MockSiteControlsDisabledTestCase(unittest.TestCase):
    def test_control_endpoints_are_hidden_when_disabled(self):
        app = create_app({"TESTING": True, "MOCK_SITE_CONTROLS_ENABLED": False})
        self.assertEqual(app.test_client().get("/__control/state").status_code, 404)


if __name__ == "__main__":
    unittest.main()
