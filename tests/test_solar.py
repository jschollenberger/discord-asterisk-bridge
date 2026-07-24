"""solar.py XML parsing — extracts hamqsl's <solar><solardata> fields.

Guards the fix for the parser looking for a nonexistent <item> wrapper (which
made every field render as "N/A").
"""
from __future__ import annotations

import solar

# Trimmed sample mirroring hamqsl.com/solarxml.php's real structure.
_SAMPLE = """<?xml version="1.0"?>
<solar>
<solardata>
<source url="http://www.hamqsl.com/solar.html">N0NBH</source>
<updated>23 Jul 2026 0100 GMT</updated>
<solarflux>145</solarflux>
<aindex>5</aindex>
<kindex>2</kindex>
<xray>B7.2</xray>
<sunspots>112</sunspots>
<solarwind>380</solarwind>
<magneticfield>3.1</magneticfield>
<calculatedconditions>
<band name="80m-40m" time="day">Good</band>
<band name="80m-40m" time="night">Fair</band>
<band name="30m-20m" time="day">Good</band>
</calculatedconditions>
<calculatedvhfconditions>
<phenomenon name="vhf-aurora" location="northern_hemi">Band Closed</phenomenon>
</calculatedvhfconditions>
</solardata>
</solar>"""


def test_parse_extracts_fields_from_solardata():
    d = solar._parse_solar_xml(_SAMPLE)
    assert d["solar_flux"] == "145"
    assert d["a_index"]    == "5"
    assert d["k_index"]    == "2"
    assert d["sunspots"]   == "112"
    assert d["updated"]    == "23 Jul 2026 0100 GMT"
    assert d["bands_day"]["80m-40m"]   == "Good"
    assert d["bands_night"]["80m-40m"] == "Fair"
    assert d["vhf"] and "Aurora" in d["vhf"][0]


def test_parse_missing_solardata_yields_all_na():
    d = solar._parse_solar_xml("<solar></solar>")
    assert d["solar_flux"] == "N/A"
    assert d["k_index"]    == "N/A"
    assert d["bands_day"]  == {}
