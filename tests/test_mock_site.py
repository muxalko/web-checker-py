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

    def test_index_links_to_bcparks_mock(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/bcparks/dayuse/"', response.get_data(as_text=True))

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

    def test_bcparks_catalog_matches_captured_production_options(self):
        parks = self.client.get("/bcparks/api/park").get_json()
        self.assertEqual(
            [(park["sk"], park["status"]) for park in parks],
            [
                ("0007", "open"),
                ("0008", "open"),
                ("0015", "closed"),
                ("0363", "open"),
            ],
        )

        expected_facilities = {
            "0007": {
                "Cheakamus": {"AM": {"max": 49}, "PM": {"max": 39}},
                "Diamond Head": {"DAY": {"max": 55}},
                "Rubble Creek": {"DAY": {"max": 230}},
            },
            "0008": {
                "Alouette Lake Boat Launch Parking": {"DAY": {"max": 100}},
                "Alouette Lake South Beach Day-Use Parking Lot": {
                    "AM": {"max": 790},
                    "PM": {"max": 420},
                },
                "Gold Creek Parking Lot": {
                    "AM": {"max": 110},
                    "PM": {"max": 60},
                },
                "West Canyon Trailhead Parking Lot": {
                    "AM": {"max": 55},
                    "PM": {"max": 30},
                },
            },
            "0363": {"Joffre Lakes": {"DAY": {"max": 570}}},
        }
        captured = {}
        for park_id in expected_facilities:
            response = self.client.get(
                "/bcparks/api/facility",
                query_string={"park": park_id, "facilities": "true"},
            )
            self.assertEqual(response.status_code, 200)
            facilities = response.get_json()
            captured[park_id] = {
                item["name"]: item["bookingTimes"] for item in facilities
            }
        self.assertEqual(captured, expected_facilities)

        facilities = self.client.get(
            "/bcparks/api/facility?park=0008&facilities=true"
        ).get_json()
        south_beach = next(
            item
            for item in facilities
            if item["name"]
            == "Alouette Lake South Beach Day-Use Parking Lot"
        )
        self.assertEqual(
            south_beach["bookingTimes"], {"AM": {"max": 790}, "PM": {"max": 420}}
        )
        self.assertEqual(south_beach["currentTime"], "2026-08-15T11:02:02.458Z")

    def test_bcparks_reservation_response_and_ui_are_available(self):
        response = self.client.get(
            "/bcparks/api/reservation",
            query_string={
                "park": "0008",
                "facility": "Alouette Lake South Beach Day-Use Parking Lot",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["2026-08-16"]["AM"]["capacity"], "Full")

        page = self.client.get(
            "/bcparks/dayuse/",
            query_string={
                "park": "0008",
                "facility": "Alouette Lake South Beach Day-Use Parking Lot",
            },
        )
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Mock BC Parks", html)
        self.assertIn("Alouette Lake South Beach", html)
        self.assertIn("This local site never makes a reservation", html)

    def test_bcparks_controls_change_availability_and_time(self):
        update = {
            "park_id": "0008",
            "facility": "Alouette Lake South Beach Day-Use Parking Lot",
            "date": "2026-08-16",
            "slot": "AM",
            "capacity": "Low",
            "max": 1,
        }
        response = self.client.patch("/__control/bcparks/reservations", json=update)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"capacity": "Low", "max": 1})

        response = self.client.patch(
            "/__control/bcparks/clock",
            json={"current_time": "2026-08-15T14:00:00Z"},
        )
        self.assertEqual(response.status_code, 200)
        facilities = self.client.get(
            "/bcparks/api/facility?park=0008&facilities=true"
        ).get_json()
        self.assertEqual(facilities[0]["currentTime"], "2026-08-15T14:00:00Z")

    def test_bcparks_clock_rolls_the_reservation_window(self):
        response = self.client.patch(
            "/__control/bcparks/clock",
            json={"current_time": "2026-08-16T14:00:00Z"},
        )
        self.assertEqual(response.status_code, 200)

        reservations = self.client.get(
            "/bcparks/api/reservation",
            query_string={
                "park": "0008",
                "facility": "Alouette Lake South Beach Day-Use Parking Lot",
            },
        ).get_json()
        self.assertEqual(
            list(reservations),
            ["2026-08-16", "2026-08-17", "2026-08-18"],
        )

    def test_bcparks_controls_simulate_closure_and_bad_responses(self):
        response = self.client.patch(
            "/__control/bcparks/parks/0008", json={"status": "closed"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "closed")

        self.client.patch("/__control/bcparks/behavior", json={"malformed": True})
        response = self.client.get("/bcparks/api/park")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "application/json")
        self.assertEqual(response.get_data(as_text=True), "{malformed")

    def test_bcparks_empty_response_control_preserves_endpoint_shape(self):
        self.client.patch("/__control/bcparks/behavior", json={"empty": True})

        self.assertEqual(self.client.get("/bcparks/api/park").get_json(), [])
        self.assertEqual(
            self.client.get(
                "/bcparks/api/reservation",
                query_string={
                    "park": "0008",
                    "facility": "Gold Creek Parking Lot",
                },
            ).get_json(),
            {},
        )

    def test_bcparks_reset_restores_captured_defaults(self):
        self.client.patch("/__control/bcparks/parks/0008", json={"status": "closed"})
        self.client.patch(
            "/__control/bcparks/clock",
            json={"current_time": "2026-08-16T18:00:00Z"},
        )

        response = self.client.post("/__control/bcparks/reset")

        self.assertEqual(response.status_code, 200)
        state = response.get_json()
        golden_ears = next(park for park in state["parks"] if park["sk"] == "0008")
        self.assertEqual(golden_ears["status"], "open")
        self.assertEqual(state["current_time"], "2026-08-15T11:02:02.458Z")


class MockSiteControlsDisabledTestCase(unittest.TestCase):
    def test_control_endpoints_are_hidden_when_disabled(self):
        app = create_app({"TESTING": True, "MOCK_SITE_CONTROLS_ENABLED": False})
        self.assertEqual(app.test_client().get("/__control/state").status_code, 404)
        self.assertEqual(
            app.test_client().get("/__control/bcparks/state").status_code, 404
        )


if __name__ == "__main__":
    unittest.main()
