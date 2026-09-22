#!/usr/bin/env python3
"""The weather module: two tools, and the shapes they accept.

    python main.py current_conditions location=Lisbon
    python main.py forecast location=Lisbon days=3
    python main.py --schemas

    echo '{"tool": "current_conditions", "args": {"location": "Lisbon"}}' \\
        | python main.py

Manifest loading, argument checking, dispatch, the answer envelope and the tool
definitions all come from :class:`sdk.Module`. What is left here is the subject
matter: which tools exist, what they accept, and what they say back.

``location`` is required and has no default. This module has no idea where the
speaker is standing and should not pretend to — knowing that is the assistant's
job, and it passes the answer in. Asked for the weather with nowhere named, the
module says so rather than guessing at a city.

``PLACE`` is the one piece of security this module owns, because only it knows
what its own arguments are for. It admits the letters, spaces and punctuation that
real place names use — ``São Paulo``, ``Zürich``, ``N'Djamena``, ``Winston-Salem``
— and nothing that could reach into a URL, a shell or a path.
"""

from __future__ import annotations

import tools
from sdk import Choice, Module, Number, Text, tool

PLACE = r"^[\w ,.'’-]+$"

LOCATION = Text(
    max_length=64,
    pattern=PLACE,
    description=(
        "Town, city or region to report on. Required — pass where the speaker is, "
        "or the place they named."
    ),
    missing="I'm not sure which place you mean.",
)
UNITS = Choice(
    "metric",
    "imperial",
    default_from="settings.units",
    description="Celsius and kilometres, or Fahrenheit and miles.",
)


class Weather(Module):
    """Current conditions and a short forecast, from Open-Meteo."""

    @tool(
        "Temperature, what it feels like, and the conditions outside right now.",
        location=LOCATION,
        units=UNITS,
    )
    def current_conditions(self, location: str, units: str) -> dict:
        return tools.current_conditions(location, units, self.timeout)

    @tool(
        "The next few days: conditions, high and low, and the chance of rain.",
        location=LOCATION,
        units=UNITS,
        days=Number(
            minimum=1,
            maximum=7,
            default=3,
            description="How many days to report, counting today.",
        ),
    )
    def forecast(self, location: str, units: str, days: int) -> dict:
        return tools.forecast(location, days, units, self.timeout)


if __name__ == "__main__":
    raise SystemExit(Weather().run())
