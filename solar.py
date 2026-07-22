"""
K2BR Repeater Bot
Copyright (C) 2026 Jason Schollenberger / KD2QED

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

solar.py — Solar/HF-propagation data from hamqsl.com.

Fetches and parses the hamqsl.com solar XML feed into a plain dict of the
values the /solar command renders (solar flux, K/A index, X-ray, sunspots,
solar wind, and per-band HF day/night conditions plus VHF phenomena). Purely
a data source — the Discord embed is built by the caller, mirroring how qrz.py
returns lookup data and the bot owns the presentation.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import aiohttp

SOLAR_URL = "https://www.hamqsl.com/solarxml.php"


async def fetch_solar() -> dict:
    """Fetch propagation data from hamqsl.com and return it as a dict."""
    async with aiohttp.ClientSession() as s:
        async with s.get(SOLAR_URL, timeout=aiohttp.ClientTimeout(total=12)) as r:
            text = await r.text()

    root = ET.fromstring(text)
    item = root.find(".//item")
    if item is None:
        item = root

    def g(tag: str) -> str:
        el = item.find(tag)
        return el.text.strip() if el is not None and el.text else "N/A"

    bands_day:   dict[str, str] = {}
    bands_night: dict[str, str] = {}
    for band in item.findall("calculatedconditions/band"):
        name = band.get("name", "")
        t    = band.get("time", "")
        val  = band.text.strip() if band.text else "N/A"
        (bands_day if t == "day" else bands_night)[name] = val

    vhf_lines = []
    for ph in item.findall("calculatedvhfconditions/phenomenon"):
        name = ph.get("name", "").replace("-", " ").title()
        loc  = ph.get("location", "").replace("_", " ").title()
        val  = ph.text.strip() if ph.text else "N/A"
        vhf_lines.append(f"**{name}** ({loc}): {val}")

    return {
        "solar_flux":  g("solarflux"),
        "a_index":     g("aindex"),
        "k_index":     g("kindex"),
        "x_ray":       g("xray"),
        "sunspots":    g("sunspots"),
        "solar_wind":  g("solarwind"),
        "mag_field":   g("magneticfield"),
        "updated":     g("updated"),
        "bands_day":   bands_day,
        "bands_night": bands_night,
        "vhf":         vhf_lines,
    }
