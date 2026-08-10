"""A deterministic mock reservation website with development-only controls."""

import os
import threading
import time
from copy import deepcopy
from datetime import date as date_type
from datetime import datetime

from flask import Flask, abort, jsonify, render_template, request

DEFAULT_OPPORTUNITIES = (
    {
        "key": "morning-pass",
        "title": "Morning Adventure Pass",
        "time": "09:00",
        "availability": "available",
        "capacity": 4,
        "enabled": True,
    },
    {
        "key": "midday-pass",
        "title": "Midday Adventure Pass",
        "time": "11:00",
        "availability": "sold-out",
        "capacity": 0,
        "enabled": True,
    },
    {
        "key": "afternoon-pass",
        "title": "Afternoon Adventure Pass",
        "time": "13:00",
        "availability": "available",
        "capacity": 2,
        "enabled": True,
    },
)

VALID_AVAILABILITY = {"available", "sold-out", "unknown"}


class MockReservationState:
    """Thread-safe, in-memory state for one mock-site process."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self._opportunities = {
                item["key"]: deepcopy(item) for item in DEFAULT_OPPORTUNITIES
            }
            self._behavior = {
                "delay_seconds": 0.0,
                "status_code": 200,
                "malformed": False,
            }

    def snapshot(self, reservation_date):
        with self._lock:
            opportunities = []
            for item in self._opportunities.values():
                if not item["enabled"]:
                    continue
                opportunity = deepcopy(item)
                opportunity["id"] = "{}-{}".format(
                    reservation_date.isoformat(), item["key"]
                )
                opportunity["starts_at"] = "{}T{}:00".format(
                    reservation_date.isoformat(), item["time"]
                )
                opportunities.append(opportunity)
            return opportunities, deepcopy(self._behavior)

    def update_opportunity(self, key, changes):
        with self._lock:
            if key not in self._opportunities:
                return None
            self._opportunities[key].update(changes)
            return deepcopy(self._opportunities[key])

    def update_behavior(self, changes):
        with self._lock:
            self._behavior.update(changes)
            return deepcopy(self._behavior)

    def control_snapshot(self):
        with self._lock:
            return {
                "opportunities": deepcopy(list(self._opportunities.values())),
                "behavior": deepcopy(self._behavior),
            }


def _controls_enabled(app):
    return app.config["MOCK_SITE_CONTROLS_ENABLED"]


def _require_controls(app):
    if not _controls_enabled(app):
        abort(404)


def _json_object():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        abort(400, description="Request body must be a JSON object")
    return body


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        MOCK_SITE_CONTROLS_ENABLED=(
            os.getenv("MOCK_SITE_CONTROLS_ENABLED", "false").lower() == "true"
        )
    )
    if config:
        app.config.update(config)

    state = MockReservationState()
    app.extensions["mock_reservation_state"] = state

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/book/<opportunity_id>")
    def book(opportunity_id):
        return render_template("book.html", opportunity_id=opportunity_id)

    @app.get("/reservations/<reservation_date>")
    def reservations(reservation_date):
        try:
            parsed_date = date_type.fromisoformat(reservation_date)
        except ValueError:
            abort(404)

        opportunities, behavior = state.snapshot(parsed_date)
        if behavior["delay_seconds"]:
            time.sleep(behavior["delay_seconds"])
        if behavior["status_code"] != 200:
            abort(behavior["status_code"])
        if behavior["malformed"]:
            return render_template(
                "malformed.html",
                reservation_date=parsed_date,
                opportunities=opportunities,
            )
        return render_template(
            "reservations.html",
            reservation_date=parsed_date,
            opportunities=opportunities,
        )

    @app.get("/__control/state")
    def control_state():
        _require_controls(app)
        return jsonify(state.control_snapshot())

    @app.post("/__control/reset")
    def control_reset():
        _require_controls(app)
        state.reset()
        return jsonify(state.control_snapshot())

    @app.patch("/__control/opportunities/<key>")
    def control_opportunity(key):
        _require_controls(app)
        body = _json_object()
        allowed = {"title", "time", "availability", "capacity", "enabled"}
        unexpected = set(body) - allowed
        if unexpected:
            abort(
                400,
                description="Unsupported fields: {}".format(
                    ", ".join(sorted(unexpected))
                ),
            )
        if "availability" in body and body["availability"] not in VALID_AVAILABILITY:
            abort(400, description="Invalid availability value")
        if "capacity" in body and (
            not isinstance(body["capacity"], int) or body["capacity"] < 0
        ):
            abort(400, description="Capacity must be a non-negative integer")
        if "enabled" in body and not isinstance(body["enabled"], bool):
            abort(400, description="Enabled must be a boolean")
        if "time" in body:
            try:
                datetime.strptime(body["time"], "%H:%M")
            except (TypeError, ValueError):
                abort(400, description="Time must use HH:MM format")

        updated = state.update_opportunity(key, body)
        if updated is None:
            abort(404)
        return jsonify(updated)

    @app.patch("/__control/behavior")
    def control_behavior():
        _require_controls(app)
        body = _json_object()
        allowed = {"delay_seconds", "status_code", "malformed"}
        unexpected = set(body) - allowed
        if unexpected:
            abort(
                400,
                description="Unsupported fields: {}".format(
                    ", ".join(sorted(unexpected))
                ),
            )
        if "delay_seconds" in body and (
            not isinstance(body["delay_seconds"], (int, float))
            or isinstance(body["delay_seconds"], bool)
            or not 0 <= body["delay_seconds"] <= 10
        ):
            abort(400, description="Delay must be between 0 and 10 seconds")
        if "status_code" in body and (
            not isinstance(body["status_code"], int)
            or body["status_code"] < 200
            or body["status_code"] > 599
        ):
            abort(400, description="Status code must be between 200 and 599")
        if "malformed" in body and not isinstance(body["malformed"], bool):
            abort(400, description="Malformed must be a boolean")
        return jsonify(state.update_behavior(body))

    return app


app = create_app()
