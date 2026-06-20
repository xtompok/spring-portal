"""Flow derivation harness.

Flow is a derived, fully-rebuildable series. The flow meter is power-gated on the
`cam` output (cam_pwr), powered ~15 s every 60 s, and `pulses` is a per-interval
count, so flow is only meaningful inside a cam_pwr window after the meter spins up.

This module provides the *harness*: it reads raw `lab` from the DB, groups rows into
contiguous cam_pwr=true windows, drops settling samples, and upserts one row per
window into `flow` (NULL when the window is invalid). The actual pulses->flow
computation is left as a STUB (`compute_flow`) to be filled in later.

`flow` table and its config knobs are created by migration 003.
"""

from datetime import timedelta

from . import db


def _config(conn) -> dict:
	with conn.cursor() as cur:
		cur.execute("SELECT key, value FROM config")
		return dict(cur.fetchall())


def _windows(rows, max_gap_s: float):
	"""Yield lists of consecutive cam_pwr=true rows, split on gaps > max_gap_s.
	rows: iterable of (time, uptime, cam_pwr, pulses) ordered by time."""
	current = []
	prev_t = None
	for t, uptime, cam, pulses in rows:
		if not cam:
			if current:
				yield current
				current = []
			prev_t = None
			continue
		if prev_t is not None and (t - prev_t).total_seconds() > max_gap_s:
			if current:
				yield current
				current = []
		current.append((t, uptime, pulses))
		prev_t = t
	if current:
		yield current


def compute_flow(window, cfg) -> tuple:
	"""Given one cam_pwr window (list of (time, uptime, pulses)) and the config
	dict, return (pulses_sum_or_None, valid_bool, n_samples_used, stable_span_s).

	Calibration K is intentionally NOT applied here — it lives in the `v_flow`
	view, so recalibrating never requires a rebuild. This harness only decides
	which samples are trustworthy and sums their pulses.
	"""
	if not window:
		return (None, False, 0, 0.0)
	if len(window) < 2:
		return (None, False, 0, 0.0)
	
	atime, uptime, pulses = window[1]
	timediff = uptime - window[0][1]
	return (pulses, True, 1, timediff/1000)



_UPSERT = """
INSERT INTO flow (time, pulses_sum, valid, n_samples, window_s)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (time) DO UPDATE SET
  pulses_sum = EXCLUDED.pulses_sum,
  valid	  = EXCLUDED.valid,
  n_samples  = EXCLUDED.n_samples,
  window_s   = EXCLUDED.window_s
"""


def rebuild(since=None, max_gap_s: float = 30.0, echo=print) -> int:
	"""(Re)compute flow from lab. Derived data is always safe to recompute.
	`since` optionally limits to lab rows at/after a timestamp."""
	conn = db.connect()
	written = 0
	try:
		cfg = _config(conn)
		with conn.cursor() as cur:
			if since:
				cur.execute(
					"SELECT time, uptime, cam_pwr, pulses FROM lab "
					"WHERE time >= %s ORDER BY time", (since,),
				)
			else:
				cur.execute(
					"SELECT time, uptime, cam_pwr, pulses FROM lab ORDER BY time"
				)
			rows = cur.fetchall()

		for window in _windows(rows, max_gap_s):
			pulses_sum, valid, n, _stable_span = compute_flow(window, cfg)
			t_mid = window[0][0] + (window[-1][0] - window[0][0]) / 2
			span = (window[-1][0] - window[0][0]).total_seconds()
			with conn.cursor() as cur:
				cur.execute(_UPSERT, (t_mid, pulses_sum, valid, n, span))
			written += 1
		conn.commit()
	finally:
		conn.close()
	echo(f"flow: wrote {written} window(s)")
	return written
