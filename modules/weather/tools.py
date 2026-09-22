"""What the weather module can actually do: two tools and one seam.

``current_conditions`` and ``forecast`` are the operations the broker offers the
model. :func:`fetch_json` is the only place either of them touches the network,
so when the broker takes egress over — holding the socket itself, and enforcing
the allowlist somewhere a module cannot reach — this is the single function that
changes and the tools stay as they are.

Open-Meteo needs no API key and covers the whole world, which is why it is here
rather than a national service. ``api.weather.gov`` looks like the obvious first
choice and answers 404 for anywhere outside the United States.

The tools return the sentence to be spoken as well as the numbers behind it. That
is deliberate: a model asked to phrase a temperature will sooner or later phrase
one nobody measured, so the facts are worded here and the model only relays them.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

GEOCODER = "geocoding-api.open-meteo.com"
FORECASTER = "api.open-meteo.com"
USER_AGENT = "nova-weather/0.1 (personal assistant module)"
MAX_BYTES = 256 * 1024

UNITS = {
    "metric": {
        "temperature": "celsius",
        "wind_speed": "kmh",
        "degrees": "degrees",
        "wind": "kilometres an hour",
    },
    "imperial": {
        "temperature": "fahrenheit",
        "wind_speed": "mph",
        "degrees": "degrees",
        "wind": "miles an hour",
    },
}

CONDITIONS = {
    0: "clear",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "freezing fog",
    51: "drizzling lightly",
    53: "drizzling",
    55: "drizzling heavily",
    56: "freezing drizzle",
    57: "heavy freezing drizzle",
    61: "raining lightly",
    63: "raining",
    65: "raining heavily",
    66: "freezing rain",
    67: "heavy freezing rain",
    71: "snowing lightly",
    73: "snowing",
    75: "snowing heavily",
    77: "snow grains",
    80: "showery",
    81: "showery",
    82: "violent showers",
    85: "light snow showers",
    86: "heavy snow showers",
    95: "thundery",
    96: "thundery with hail",
    99: "thundery with heavy hail",
}

WINDY = 25.0
FEELS_DIFFERENT = 3.0
WORTH_MENTIONING_RAIN = 30


class WeatherError(RuntimeError):
    """Something went wrong that the speaker should hear about."""

    def __init__(self, message: str, spoken: str):
        super().__init__(message)
        self.spoken = spoken


def describe(code: int | None) -> str:
    """Turn a WMO weather code into something worth saying out loud."""
    return CONDITIONS.get(code, "hard to say")


def fetch_json(host: str, path: str, params: dict, timeout: float) -> dict:
    """The module's only door to the outside world.

    The host and path are fixed by the caller and never built from a tool
    argument; everything the speaker said travels as a urlencoded query value, so
    a place name cannot reach into the URL itself.
    """
    url = f"https://{host}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise WeatherError(
            f"{host} answered {exc.code}", "The weather service turned me down."
        ) from exc
    except urllib.error.URLError as exc:
        raise WeatherError(
            f"cannot reach {host}: {exc.reason}", "I couldn't reach the weather service."
        ) from exc
    except TimeoutError as exc:
        raise WeatherError(
            f"{host} timed out", "The weather service took too long."
        ) from exc

    if len(payload) > MAX_BYTES:
        raise WeatherError(
            f"{host} sent more than {MAX_BYTES} bytes", "That answer was too big."
        )
    try:
        return json.loads(payload)
    except ValueError as exc:
        raise WeatherError(
            f"{host} sent something that is not JSON", "I got a garbled answer."
        ) from exc


def locate(place: str, timeout: float) -> dict:
    """Find one place by name, or say plainly that it is not on the map."""
    found = fetch_json(
        GEOCODER,
        "/v1/search",
        {"name": place, "count": 1, "language": "en", "format": "json"},
        timeout,
    )
    results = found.get("results") or []
    if not results:
        raise WeatherError(
            f"no place called {place!r}", f"I couldn't find anywhere called {place}."
        )
    first = results[0]
    label = first.get("name") or place
    country = first.get("country")
    return {
        "label": f"{label}, {country}" if country else label,
        "name": label,
        "country": country,
        "latitude": first["latitude"],
        "longitude": first["longitude"],
        "timezone": first.get("timezone"),
    }


def _round(value) -> int | None:
    return None if value is None else int(round(value))


def _units(units: str) -> dict:
    return UNITS.get(units, UNITS["metric"])


def current_conditions(location: str, units: str, timeout: float) -> dict:
    """What it is like outside right now."""
    words = _units(units)
    place = locate(location, timeout)
    answer = fetch_json(
        FORECASTER,
        "/v1/forecast",
        {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "weather_code,wind_speed_10m",
            "temperature_unit": words["temperature"],
            "wind_speed_unit": words["wind_speed"],
            "timezone": "auto",
        },
        timeout,
    )
    now = answer.get("current") or {}
    temperature = _round(now.get("temperature_2m"))
    feels_like = _round(now.get("apparent_temperature"))
    wind = now.get("wind_speed_10m")
    conditions = describe(now.get("weather_code"))

    said = [f"It's {temperature} {words['degrees']} and {conditions} in {place['name']}."]
    if (
        temperature is not None
        and feels_like is not None
        and abs(feels_like - temperature) >= FEELS_DIFFERENT
    ):
        said.append(f"It feels more like {feels_like}.")
    if wind is not None and wind >= WINDY:
        said.append(f"Wind around {_round(wind)} {words['wind']}.")

    return {
        "speech": " ".join(said),
        "data": {
            "place": place["label"],
            "temperature": temperature,
            "feels_like": feels_like,
            "humidity": now.get("relative_humidity_2m"),
            "wind_speed": _round(wind),
            "conditions": conditions,
            "units": units,
            "observed_at": now.get("time"),
        },
    }


def _day_label(iso_day: str, index: int) -> str:
    if index == 0:
        return "Today"
    if index == 1:
        return "Tomorrow"
    try:
        return date.fromisoformat(iso_day).strftime("%A")
    except ValueError:
        return iso_day


def forecast(location: str, days: int, units: str, timeout: float) -> dict:
    """The next few days, short enough to be spoken in one breath."""
    words = _units(units)
    place = locate(location, timeout)
    answer = fetch_json(
        FORECASTER,
        "/v1/forecast",
        {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max",
            "temperature_unit": words["temperature"],
            "wind_speed_unit": words["wind_speed"],
            "timezone": "auto",
            "forecast_days": days,
        },
        timeout,
    )
    daily = answer.get("daily") or {}
    stamps = daily.get("time") or []
    if not stamps:
        raise WeatherError(
            "no daily forecast came back", "I couldn't get a forecast for that place."
        )

    said = [f"In {place['name']}:"]
    entries = []
    for index, stamp in enumerate(stamps[:days]):
        low = _round((daily.get("temperature_2m_min") or [None])[index])
        high = _round((daily.get("temperature_2m_max") or [None])[index])
        rain = (daily.get("precipitation_probability_max") or [None])[index]
        conditions = describe((daily.get("weather_code") or [None])[index])
        label = _day_label(stamp, index)

        sentence = f"{label}, {conditions}, {low} to {high} {words['degrees']}"
        if rain is not None and rain >= WORTH_MENTIONING_RAIN:
            sentence += f", {rain} percent chance of rain"
        said.append(sentence + ".")
        entries.append(
            {
                "day": stamp,
                "label": label,
                "low": low,
                "high": high,
                "rain_chance": rain,
                "conditions": conditions,
            }
        )

    return {
        "speech": " ".join(said),
        "data": {"place": place["label"], "units": units, "days": entries},
    }


TOOLS = {
    "current_conditions": current_conditions,
    "forecast": forecast,
}
