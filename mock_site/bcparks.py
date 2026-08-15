"""Production-shaped BC Parks day-use mock API and development controls."""

import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Response, abort, jsonify, render_template, request

DEFAULT_CURRENT_TIME = "2026-08-15T11:02:02.458Z"
PROVIDER_TIMEZONE = ZoneInfo("America/Vancouver")

DEFAULT_PARKS = (
    {
        "pk": "park",
        "sk": "0007",
        "name": "Garibaldi Provincial Park",
        "status": "open",
        "visible": True,
        "specialClosure": None,
        "specialClosureText": None,
        "winterWarning": False,
    },
    {
        "pk": "park",
        "sk": "0008",
        "name": "Golden Ears Provincial Park",
        "status": "open",
        "visible": True,
        "specialClosure": None,
        "specialClosureText": None,
        "winterWarning": None,
    },
    {
        "pk": "park",
        "sk": "0015",
        "name": "Mount Seymour Provincial Park",
        "status": "closed",
        "visible": True,
        "specialClosure": None,
        "specialClosureText": None,
        "winterWarning": True,
    },
    {
        "pk": "park",
        "sk": "0363",
        "name": "Joffre Lakes Provincial Park",
        "status": "open",
        "visible": True,
        "specialClosure": None,
        "specialClosureText": None,
        "winterWarning": None,
    },
)

ALL_DAYS = {str(day): True for day in range(1, 8)}
FRIDAY_TO_MONDAY = {
    "1": True,
    "2": False,
    "3": False,
    "4": False,
    "5": True,
    "6": True,
    "7": True,
}


def _facility(park_id, name, facility_type, booking_days, booking_times):
    return {
        "pk": f"facility::{park_id}",
        "sk": name,
        "name": name,
        "type": facility_type,
        "visible": True,
        "status": {"state": "open", "stateReason": None},
        "bookingDaysAhead": 2,
        "bookingOpeningHour": 7,
        "bookingDays": deepcopy(booking_days),
        "bookingTimes": {
            slot: {"max": capacity} for slot, capacity in booking_times.items()
        },
        "bookableHolidays": {},
        "qrcode": True,
        "isUpdating": False,
    }


DEFAULT_FACILITIES = {
    "0007": (
        _facility("0007", "Cheakamus", "Parking", ALL_DAYS, {"AM": 49, "PM": 39}),
        _facility("0007", "Diamond Head", "Parking", FRIDAY_TO_MONDAY, {"DAY": 55}),
        _facility("0007", "Rubble Creek", "Parking", FRIDAY_TO_MONDAY, {"DAY": 230}),
    ),
    "0008": (
        _facility(
            "0008",
            "Alouette Lake Boat Launch Parking",
            "Parking",
            FRIDAY_TO_MONDAY,
            {"DAY": 100},
        ),
        _facility(
            "0008",
            "Alouette Lake South Beach Day-Use Parking Lot",
            "Parking",
            FRIDAY_TO_MONDAY,
            {"AM": 790, "PM": 420},
        ),
        _facility(
            "0008",
            "Gold Creek Parking Lot",
            "Parking",
            FRIDAY_TO_MONDAY,
            {"AM": 110, "PM": 60},
        ),
        _facility(
            "0008",
            "West Canyon Trailhead Parking Lot",
            "Parking",
            FRIDAY_TO_MONDAY,
            {"AM": 55, "PM": 30},
        ),
    ),
    "0363": (_facility("0363", "Joffre Lakes", "Trail", ALL_DAYS, {"DAY": 570}),),
}


def _availability(slots, *, pass_limit):
    return {
        "2026-08-15": {slot: {"capacity": "Full", "max": 0} for slot in slots},
        "2026-08-16": {slot: {"capacity": "Full", "max": 0} for slot in slots},
        "2026-08-17": {slot: {"capacity": "High", "max": pass_limit} for slot in slots},
    }


DEFAULT_RESERVATIONS = {
    ("0007", "Cheakamus"): _availability(("AM", "PM"), pass_limit=1),
    ("0007", "Diamond Head"): _availability(("DAY",), pass_limit=1),
    ("0007", "Rubble Creek"): _availability(("DAY",), pass_limit=1),
    ("0008", "Alouette Lake Boat Launch Parking"): _availability(
        ("DAY",), pass_limit=1
    ),
    ("0008", "Alouette Lake South Beach Day-Use Parking Lot"): _availability(
        ("AM", "PM"), pass_limit=1
    ),
    ("0008", "Gold Creek Parking Lot"): _availability(("AM", "PM"), pass_limit=1),
    ("0008", "West Canyon Trailhead Parking Lot"): _availability(
        ("AM", "PM"), pass_limit=1
    ),
    ("0363", "Joffre Lakes"): _availability(("DAY",), pass_limit=4),
}

DEFAULT_CONFIG = {
    "debugMode": False,
    "API_PUBLIC_PATH": "/api",
    "PARKING_PASS_LIMIT": 1,
    "ADVANCE_BOOKING_LIMIT": 3,
    "ADVANCE_BOOKING_HOUR": 7,
    "TRAIL_PASS_LIMIT": 4,
    "ENVIRONMENT": "mock",
    "API_LOCATION": "http://mock-site:8080/bcparks",
    "API_PATH": "/api",
}


class BCParksMockState:
    """Thread-safe state for the production-shaped mock contract."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self._parks = {item["sk"]: deepcopy(item) for item in DEFAULT_PARKS}
            self._facilities = {
                park_id: {item["name"]: deepcopy(item) for item in facilities}
                for park_id, facilities in DEFAULT_FACILITIES.items()
            }
            self._reservations = deepcopy(DEFAULT_RESERVATIONS)
            self._current_time = DEFAULT_CURRENT_TIME
            self._behavior = {
                "delay_seconds": 0.0,
                "status_code": 200,
                "malformed": False,
                "empty": False,
            }

    def response_behavior(self):
        with self._lock:
            return deepcopy(self._behavior)

    def parks(self):
        with self._lock:
            return deepcopy(list(self._parks.values()))

    def facilities(self, park_id):
        with self._lock:
            facilities = deepcopy(list(self._facilities.get(park_id, {}).values()))
            for item in facilities:
                item["currentTime"] = self._current_time
            return facilities

    def reservations(self, park_id, facility):
        with self._lock:
            value = self._reservations.get((park_id, facility))
            return None if value is None else deepcopy(value)

    def snapshot(self):
        with self._lock:
            return {
                "parks": deepcopy(list(self._parks.values())),
                "facilities": deepcopy(self._facilities),
                "reservations": {
                    f"{park_id}::{facility}": deepcopy(value)
                    for (park_id, facility), value in self._reservations.items()
                },
                "current_time": self._current_time,
                "behavior": deepcopy(self._behavior),
            }

    def set_current_time(self, value):
        with self._lock:
            previous_date = _provider_date(self._current_time)
            next_date = _provider_date(value)
            date_delta = next_date - previous_date
            if date_delta:
                self._reservations = {
                    key: {
                        (
                            datetime.fromisoformat(raw_date).date() + date_delta
                        ).isoformat(): slots
                        for raw_date, slots in reservations.items()
                    }
                    for key, reservations in self._reservations.items()
                }
            self._current_time = value
            return self._current_time

    def update_behavior(self, changes):
        with self._lock:
            self._behavior.update(changes)
            return deepcopy(self._behavior)

    def update_park(self, park_id, changes):
        with self._lock:
            if park_id not in self._parks:
                return None
            self._parks[park_id].update(changes)
            return deepcopy(self._parks[park_id])

    def update_facility(self, park_id, facility, changes):
        with self._lock:
            item = self._facilities.get(park_id, {}).get(facility)
            if item is None:
                return None
            changes = deepcopy(changes)
            if "status" in changes:
                item["status"]["state"] = changes.pop("status")
            item.update(changes)
            updated = deepcopy(item)
            updated["currentTime"] = self._current_time
            return updated

    def update_reservation(self, park_id, facility, reservation_date, slot, value):
        with self._lock:
            reservations = self._reservations.get((park_id, facility))
            if reservations is None:
                return None
            slots = reservations.get(reservation_date)
            if slots is None or slot not in slots:
                return None
            slots[slot] = deepcopy(value)
            return deepcopy(slots[slot])


def _provider_date(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(PROVIDER_TIMEZONE).date()


def register_bcparks_mock(app, *, require_controls, json_object):
    """Register production-shaped public and development-only control routes."""
    state = BCParksMockState()
    app.extensions["bcparks_mock_state"] = state

    def apply_behavior(empty_value):
        behavior = state.response_behavior()
        if behavior["delay_seconds"]:
            time.sleep(behavior["delay_seconds"])
        if behavior["status_code"] != 200:
            abort(behavior["status_code"])
        if behavior["malformed"]:
            return Response("{malformed", status=200, content_type="application/json")
        if behavior["empty"]:
            return jsonify(empty_value)
        return None

    @app.get("/bcparks/dayuse/")
    def bcparks_dayuse():
        parks = state.parks()
        selected_park = request.args.get("park")
        facilities = state.facilities(selected_park) if selected_park else []
        selected_facility = request.args.get("facility")
        reservations = (
            state.reservations(selected_park, selected_facility)
            if selected_park and selected_facility
            else None
        )
        return render_template(
            "bcparks_dayuse.html",
            parks=parks,
            selected_park=selected_park,
            facilities=facilities,
            selected_facility=selected_facility,
            reservations=reservations,
        )

    @app.get("/bcparks/api/config")
    def bcparks_config():
        special = apply_behavior({})
        return special or jsonify(deepcopy(DEFAULT_CONFIG))

    @app.get("/bcparks/api/park")
    def bcparks_parks():
        special = apply_behavior([])
        return special or jsonify(state.parks())

    @app.get("/bcparks/api/facility")
    def bcparks_facilities():
        special = apply_behavior([])
        if special is not None:
            return special
        park_id = request.args.get("park")
        if request.args.get("facilities") != "true" or not park_id:
            abort(400)
        facilities = state.facilities(park_id)
        if not facilities:
            abort(404)
        return jsonify(facilities)

    @app.get("/bcparks/api/reservation")
    def bcparks_reservations():
        special = apply_behavior({})
        if special is not None:
            return special
        park_id = request.args.get("park")
        facility = request.args.get("facility")
        if not park_id or not facility:
            abort(400)
        reservations = state.reservations(park_id, facility)
        if reservations is None:
            abort(404)
        return jsonify(reservations)

    @app.get("/__control/bcparks/state")
    def control_bcparks_state():
        require_controls(app)
        return jsonify(state.snapshot())

    @app.post("/__control/bcparks/reset")
    def control_bcparks_reset():
        require_controls(app)
        state.reset()
        return jsonify(state.snapshot())

    @app.patch("/__control/bcparks/clock")
    def control_bcparks_clock():
        require_controls(app)
        body = json_object()
        if set(body) != {"current_time"}:
            abort(400, description="Clock requires only current_time")
        current_time = body["current_time"]
        if not isinstance(current_time, str):
            abort(400, description="current_time must be an ISO-8601 string")
        try:
            parsed = datetime.fromisoformat(current_time.replace("Z", "+00:00"))
        except ValueError:
            abort(400, description="current_time must be an ISO-8601 string")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            abort(400, description="current_time must include a timezone")
        return jsonify(current_time=state.set_current_time(current_time))

    @app.patch("/__control/bcparks/behavior")
    def control_bcparks_behavior():
        require_controls(app)
        body = json_object()
        allowed = {"delay_seconds", "status_code", "malformed", "empty"}
        if set(body) - allowed:
            abort(400, description="Unsupported BC Parks behavior fields")
        if "delay_seconds" in body and (
            not isinstance(body["delay_seconds"], (int, float))
            or isinstance(body["delay_seconds"], bool)
            or not 0 <= body["delay_seconds"] <= 10
        ):
            abort(400, description="Delay must be between 0 and 10 seconds")
        if "status_code" in body and (
            not isinstance(body["status_code"], int)
            or (body["status_code"] != 200 and not 400 <= body["status_code"] <= 599)
        ):
            abort(400, description="Status code must be 200 or between 400 and 599")
        for field in ("malformed", "empty"):
            if field in body and not isinstance(body[field], bool):
                abort(400, description=f"{field} must be a boolean")
        return jsonify(state.update_behavior(body))

    @app.patch("/__control/bcparks/parks/<park_id>")
    def control_bcparks_park(park_id):
        require_controls(app)
        body = json_object()
        if set(body) - {"status", "visible"}:
            abort(400, description="Unsupported park fields")
        if "status" in body and body["status"] not in {"open", "closed"}:
            abort(400, description="Park status must be open or closed")
        if "visible" in body and not isinstance(body["visible"], bool):
            abort(400, description="Park visible must be a boolean")
        updated = state.update_park(park_id, body)
        if updated is None:
            abort(404)
        return jsonify(updated)

    @app.patch("/__control/bcparks/facilities/<park_id>/<path:facility>")
    def control_bcparks_facility(park_id, facility):
        require_controls(app)
        body = json_object()
        if set(body) - {"status", "visible"}:
            abort(400, description="Unsupported facility fields")
        if "status" in body and body["status"] not in {"open", "closed"}:
            abort(400, description="Facility status must be open or closed")
        if "visible" in body and not isinstance(body["visible"], bool):
            abort(400, description="Facility visible must be a boolean")
        updated = state.update_facility(park_id, facility, body)
        if updated is None:
            abort(404)
        return jsonify(updated)

    @app.patch("/__control/bcparks/reservations")
    def control_bcparks_reservation():
        require_controls(app)
        body = json_object()
        required = {"park_id", "facility", "date", "slot", "capacity", "max"}
        if set(body) != required:
            abort(400, description="Reservation update has incorrect fields")
        if body["capacity"] not in {"Full", "Low", "Medium", "High"}:
            abort(400, description="Unknown capacity")
        if (
            isinstance(body["max"], bool)
            or not isinstance(body["max"], int)
            or not 0 <= body["max"] <= 100
        ):
            abort(400, description="max must be between 0 and 100")
        if (body["capacity"] == "Full") != (body["max"] == 0):
            abort(400, description="capacity and max are inconsistent")
        updated = state.update_reservation(
            body["park_id"],
            body["facility"],
            body["date"],
            body["slot"],
            {"capacity": body["capacity"], "max": body["max"]},
        )
        if updated is None:
            abort(404)
        return jsonify(updated)
