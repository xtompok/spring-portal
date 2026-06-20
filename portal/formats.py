"""Firmware-derived column specs for the lab SD `.TXT` files.

The ground truth is the firmware `dump_line()` format string, NOT the per-directory
COLUMNS.TXT (whose header is corrupted by a firmware bug and whose field count
disagrees with the data). We map data rows by field count:

  * 34 fields -> new_format_v1 (legacy): base_dir .. magn_z; angle_* truncated -> NULL
  * 37 fields -> new_format_v2 (current): adds angle_x/y/z

Old format (COLUMNS.TXT without `rtctime`) is skipped: no wall clock, unanchorable.

Verified against data_2026-06-20/ (395802 sampled rows were uniformly 34 fields).
"""

# Sanity floor: rows before this are cold-boot epoch garbage (RTC never set).
SANITY_FLOOR_EPOCH = 1_704_067_200  # 2024-01-01T00:00:00Z

# Full 37-column layout, in firmware emission order. (name, kind)
# kinds: int, bool (emitted as 0/1), float, text (1-wire address)
_FULL_SPEC = [
    ("base_dir", "int"),
    ("file_name", "int"),
    ("uptime", "int"),
    ("rtctime", "int"),
    ("router_pwr", "bool"),
    ("switch_pwr", "bool"),
    ("cam_pwr", "bool"),
    ("pulses", "int"),
    ("vin_voltage", "float"),
    ("batt_voltage", "float"),
    ("vbus", "float"),
    ("vcell1", "float"),
    ("vcell2", "float"),
    ("vcell3", "float"),
    ("vcell4", "float"),
    ("vin_current", "float"),
    ("batt_current", "float"),
    ("therm0_addr", "text"),
    ("therm0_temp", "float"),
    ("therm1_addr", "text"),
    ("therm1_temp", "float"),
    ("therm2_addr", "text"),
    ("therm2_temp", "float"),
    ("therm3_addr", "text"),
    ("therm3_temp", "float"),
    ("acc_x", "float"),
    ("acc_y", "float"),
    ("acc_z", "float"),
    ("gyro_x", "float"),
    ("gyro_y", "float"),
    ("gyro_z", "float"),
    ("magn_x", "float"),
    ("magn_y", "float"),
    ("magn_z", "float"),
    ("angle_x", "float"),
    ("angle_y", "float"),
    ("angle_z", "float"),
]

# field count -> (format_version, spec slice)
FORMATS = {
    34: ("new_format_v1", _FULL_SPEC[:34]),
    37: ("new_format_v2", _FULL_SPEC),
}

# All DB columns the parser may populate (angle_* default to None for v1).
ALL_COLUMNS = [name for name, _ in _FULL_SPEC]


class ParseError(ValueError):
    """A row could not be parsed structurally (-> quarantine)."""


def header_is_new_format(header_line: str) -> bool:
    """True if a COLUMNS.TXT header marks a new-format directory (has rtctime)."""
    return "rtctime" in header_line


def _coerce(value: str, kind: str):
    v = value.strip()
    if v == "" or v == "-":
        return None
    if kind == "text":
        return v
    if kind == "bool":
        return int(v) != 0
    if kind == "int":
        # tolerate values written with a trailing .0
        return int(float(v)) if ("." in v or "e" in v.lower()) else int(v)
    if kind == "float":
        return float(v)
    raise ParseError(f"unknown kind {kind!r}")


def parse_row(raw_line: str) -> dict:
    """Parse one data line into {column: value}. Raises ParseError if structurally
    broken (unknown field count, or a non-numeric value in a numeric column)."""
    fields = raw_line.rstrip("\n").rstrip("\r").split(";")
    n = len(fields)
    if n not in FORMATS:
        raise ParseError(f"unexpected field count {n} (expected one of {sorted(FORMATS)})")
    format_version, spec = FORMATS[n]

    row = {col: None for col in ALL_COLUMNS}
    for (name, kind), value in zip(spec, fields):
        try:
            row[name] = _coerce(value, kind)
        except (ValueError, TypeError) as exc:
            raise ParseError(f"bad {name} ({kind}): {value!r} ({exc})") from exc

    row["format_version"] = format_version
    return row
