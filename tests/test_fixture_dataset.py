"""Synthetic fixture dataset: byte-identical regeneration, validity and every declared trap.

data/fixtures/market/synthetic-us-equities-v1 is SYNTHETIC_FIXTURE data, not market data. Apart
from the regeneration checks, these tests read the committed files directly and verify them
against the manifest and independent references (zoneinfo, csv, simple as-of folds), so a
generator bug cannot also hide its own evidence.
"""

import csv
import hashlib
import io
import json
import math
import os
import re
import statistics
import subprocess
import sys
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext
from functools import cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aura.data import fixture_generator as generator

DATASET = Path(__file__).resolve().parents[1] / "data/fixtures/market/synthetic-us-equities-v1"
BARS = "bars_1d.csv"
INSTRUMENTS = "instruments.csv"
SYMBOLS = "symbol_history.csv"
UNIVERSE = "universe_membership.csv"
ACTIONS = "corporate_actions.csv"
NEW_YORK = ZoneInfo("America/New_York")
PUBLICATION_DELAY = timedelta(minutes=15)
END_OF_KNOWLEDGE = datetime(2100, 1, 1, tzinfo=UTC)
PRICE = re.compile(r"(0|[1-9][0-9]*)\.[0-9]{2}")
INSTANT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
INSTRUMENT_ID = re.compile(r"inst_syn_[0-9]{4}")
DECLARED_TRAPS = {
    "late_revision",
    "provisional_then_final",
    "missing_session",
    "impossible_ohlc",
    "identical_duplicate",
    "conflicting_duplicate",
    "out_of_order_delivery",
    "unknown_instrument",
    "mid_period_listing",
    "delisting_retained_in_history",
    "ticker_rename",
    "unsupported_split",
    "stale_tail",
}


def _reject_float(text: str) -> None:
    raise AssertionError(f"manifest.json contains a floating-point number: {text}")


@cache
def manifest() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        (DATASET / "manifest.json").read_bytes(), parse_float=_reject_float
    )
    return document


@cache
def table(name: str) -> tuple[dict[str, str], ...]:
    text = (DATASET / name).read_bytes().decode("ascii")
    return tuple(csv.DictReader(io.StringIO(text, newline=""), strict=True))


@cache
def calendar() -> tuple[date, ...]:
    definition = manifest()["calendar_definition"]
    holidays = {date.fromisoformat(day) for day in definition["holidays"]}
    day, last = (date.fromisoformat(definition[k]) for k in ("first_session", "last_session"))
    sessions = []
    while day <= last:
        if day.weekday() < 5 and day not in holidays:
            sessions.append(day)
        day += timedelta(days=1)
    return tuple(sessions)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def instant(value: str) -> datetime:
    assert INSTANT.fullmatch(value), value
    return datetime.fromisoformat(value)


def new_york(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=NEW_YORK).astimezone(UTC)


def session_of(bar: dict[str, str]) -> date:
    return instant(bar["interval_start"]).astimezone(NEW_YORK).date()


def ohlc_valid(bar: dict[str, str]) -> bool:
    open_, high, low, close = (Decimal(bar[k]) for k in ("open", "high", "low", "close"))
    return 0 < low <= min(open_, close) and max(open_, close) <= high


def bars_of(instrument_id: str) -> list[dict[str, str]]:
    return [bar for bar in table(BARS) if bar["instrument_id"] == instrument_id]


def rows_on(instrument_id: str, day: date) -> list[dict[str, str]]:
    return [bar for bar in bars_of(instrument_id) if session_of(bar) == day]


def scenario(scenario_id: str) -> dict[str, Any]:
    (found,) = (s for s in manifest()["scenarios"] if s["id"] == scenario_id)
    return found


def declared(item: dict[str, Any], role: str) -> dict[str, str]:
    """The scenario's record for a role, checked value for value against the file row."""
    (record,) = (r for r in item["records"] if r["role"] == role)
    actual = table(record["file"])[record["row"] - 1]
    assert actual == {k: v for k, v in record.items() if k not in ("file", "row", "role")}
    return actual


def trap_rows() -> set[int]:
    return {row for s in manifest()["scenarios"] for row in s["trap_rows"].get(BARS, [])}


def latest(provider_record_id: str, cutoff: datetime) -> dict[str, str] | None:
    knowable = [
        bar
        for bar in table(BARS)
        if bar["provider_record_id"] == provider_record_id
        and instant(bar["available_at"]) <= cutoff
    ]
    return max(knowable, key=lambda bar: int(bar["revision"])) if knowable else None


def members(as_of: date, cutoff: datetime) -> set[str]:
    """Fold the universe event log: the latest effective event known at the cutoff wins."""
    state: dict[str, tuple[date, str]] = {}
    for event in table(UNIVERSE):
        effective = date.fromisoformat(event["effective_from"])
        if effective <= as_of and instant(event["available_at"]) <= cutoff:
            current = state.get(event["instrument_id"])
            if current is None or effective >= current[0]:
                state[event["instrument_id"]] = (effective, event["action"])
    return {instrument for instrument, (_, action) in state.items() if action == "ADD"}


def symbol_of(instrument_id: str, day: date, cutoff: datetime) -> str | None:
    """Strict point-in-time resolution: a period's end counts only once its successor is known."""
    known = [
        r
        for r in table(SYMBOLS)
        if r["instrument_id"] == instrument_id and instant(r["available_at"]) <= cutoff
    ]
    starts = {r["effective_from"] for r in known}
    matches = []
    for r in known:
        end = r["effective_to"] if r["effective_to"] in starts else ""
        if r["effective_from"] <= day.isoformat() and (not end or day.isoformat() < end):
            matches.append(r["symbol"])
    assert len(matches) <= 1
    return matches[0] if matches else None


def active_window(instrument_id: str) -> tuple[date, date]:
    listed = date.fromisoformat(
        next(r for r in table(INSTRUMENTS) if r["instrument_id"] == instrument_id)["listed_on"]
    )
    last = calendar()[-1]
    if instrument_id == scenario("delisting_retained_in_history")["instrument_id"]:
        last = date.fromisoformat(
            scenario("delisting_retained_in_history")["facts"]["last_session"]
        )
    if instrument_id == scenario("stale_tail")["instrument_id"]:
        last = date.fromisoformat(scenario("stale_tail")["facts"]["last_bar_session"])
    return max(listed, calendar()[0]), last


# Regeneration and manifest


def test_regeneration_is_byte_identical(tmp_path: Path) -> None:
    out = tmp_path / "regenerated"
    files = generator.write_dataset(out)
    assert sorted(files) == sorted(path.name for path in DATASET.iterdir())
    declared_hashes = {**manifest()["files"], **manifest()["documents"]}
    for name, content in files.items():
        assert (DATASET / name).read_bytes() == content, name
        assert sha256((out / name).read_bytes()) == sha256(content)
        if name != "manifest.json":
            assert declared_hashes[name]["sha256"] == sha256(content)


def test_module_entry_point_regenerates_identical_files(tmp_path: Path) -> None:
    out = tmp_path / "cli"
    result = subprocess.run(
        [sys.executable, "-m", "aura.data.fixture_generator", "--out", str(out)],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env={**os.environ, "PYTHONHASHSEED": "12345"},
    )
    assert result.returncode == 0, result.stderr
    assert "not market data" in result.stdout
    for path in DATASET.iterdir():
        assert sha256((out / path.name).read_bytes()) == sha256(path.read_bytes()), path.name


def test_generation_ignores_the_ambient_decimal_context() -> None:
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_DOWN
        files = generator.build_dataset()
    assert all(files[name] == (DATASET / name).read_bytes() for name in files)


def test_manifest_lists_hashes_and_row_counts_for_every_file() -> None:
    document = manifest()
    listed = set(document["files"]) | set(document["documents"]) | {"manifest.json"}
    assert listed == {path.name for path in DATASET.iterdir()}
    for name, entry in document["files"].items():
        data = (DATASET / name).read_bytes()
        assert entry == {"sha256": sha256(data), "rows": len(table(name))}
        details = document["file_details"][name]
        assert details["bytes"] == len(data)
        header = data.decode("ascii").split("\n", 1)[0].split(",")
        assert [column["name"] for column in details["columns"]] == header
    readme = (DATASET / "README.md").read_bytes()
    assert document["documents"]["README.md"] == {
        "sha256": sha256(readme),
        "rows": readme.count(b"\n"),
    }
    stats = document["stats"]
    assert stats["bar_rows"] == len(table(BARS))
    assert stats["data_rows"] == sum(entry["rows"] for entry in document["files"].values())


def test_manifest_declares_identity_rights_and_calendar() -> None:
    document = manifest()
    assert document["dataset_id"] == DATASET.name == "synthetic-us-equities-v1"
    assert document["version"] == 1
    assert document["rights"] == {
        "license": "MIT (repo)",
        "third_party_content": "none",
        "provenance_class": "SYNTHETIC_FIXTURE",
        "not_market_data": True,
    }
    assert document["generator"]["module"] == "aura.data.fixture_generator"
    assert document["generator"]["version"] == generator.GENERATOR_VERSION
    assert document["generator"]["seed"] == generator.SEED
    assert document["calendar"] == "FIXTURE_WEEKDAY_V1"
    definition = document["calendar_definition"]
    assert definition["id"] == "FIXTURE_WEEKDAY_V1"
    assert definition["exchange_calendar"] is False
    assert definition["sessions"] == len(calendar()) == 380
    assert "NOT MARKET DATA" in document["title"]
    assert "NOT MARKET DATA" in (DATASET / "README.md").read_text(encoding="ascii")


def test_manifest_time_ranges_match_the_files() -> None:
    bars = table(BARS)
    assert manifest()["event_range"]["min"] == min(bar["interval_start"] for bar in bars)
    assert manifest()["event_range"]["max"] == max(bar["interval_end"] for bar in bars)
    assert manifest()["availability_range"]["min"] == min(bar["available_at"] for bar in bars)
    assert manifest()["availability_range"]["max"] == max(bar["available_at"] for bar in bars)
    reference = [
        r["available_at"] for name in (INSTRUMENTS, SYMBOLS, UNIVERSE, ACTIONS) for r in table(name)
    ]
    assert manifest()["reference_ranges"]["available_at"] == {
        "min": min(reference),
        "max": max(reference),
    }


def test_files_are_ascii_lf_and_float_free() -> None:
    for path in DATASET.iterdir():
        data = path.read_bytes()
        data.decode("ascii")
        assert b"\r" not in data and data.endswith(b"\n"), path.name
        if path.suffix == ".csv":
            assert b'"' not in data, path.name
    for bar in table(BARS):
        assert all(PRICE.fullmatch(bar[k]) for k in ("open", "high", "low", "close")), bar
        assert bar["volume"].isdigit(), bar
    manifest()  # parse_float rejects any float in the manifest


def test_symbols_cannot_collide_with_real_tickers() -> None:
    """US exchange tickers are letters (plus class-suffix punctuation); these all hold a digit."""
    symbols = {r["symbol"] for r in table(SYMBOLS)}
    assert symbols == {f"SYN{n:02d}" for n in range(1, 11)} | {"SYNX1", "SYNX2", "SYN7R"}
    for symbol in symbols:
        assert re.fullmatch(r"SYN[0-9A-Z]{2}", symbol) and any(c.isdigit() for c in symbol)
    ids = {
        r["instrument_id"]
        for name in (INSTRUMENTS, SYMBOLS, UNIVERSE, ACTIONS, BARS)
        for r in table(name)
    }
    assert all(INSTRUMENT_ID.fullmatch(instrument) for instrument in ids)
    assert all(r["name"].startswith("Synthetic Fixture ") for r in table(INSTRUMENTS))
    assert len(table(INSTRUMENTS)) == 12
    assert [r["security_type"] for r in table(INSTRUMENTS)] == ["EQUITY"] * 10 + ["ETF"] * 2


# Calendar and time semantics


def test_calendar_is_weekdays_minus_declared_holidays() -> None:
    sessions = calendar()
    assert len(sessions) == 380 and sessions[0] == date(2024, 1, 2)
    definition = manifest()["calendar_definition"]
    assert definition["last_session"] == sessions[-1].isoformat()
    assert all(date.fromisoformat(d).weekday() < 5 for d in definition["holidays"])
    assert {session_of(bar) for bar in table(BARS)} == set(sessions)
    assert manifest()["warm_up"]["first_session_with_full_warm_up"] == sessions[199].isoformat()


def test_bars_cover_new_york_sessions_stored_in_utc() -> None:
    offsets = set()
    for bar in table(BARS):
        start, end = instant(bar["interval_start"]), instant(bar["interval_end"])
        day = session_of(bar)
        assert start == new_york(day, time(9, 30)) and end == new_york(day, time(16, 0)), bar
        assert instant(bar["available_at"]) >= start
        offsets.add(start.astimezone(NEW_YORK).utcoffset())
    assert offsets == {timedelta(hours=-5), timedelta(hours=-4)}
    for transition in manifest()["calendar_definition"]["dst_transitions"]:
        after = date.fromisoformat(transition["first_session_after"])
        assert transition["interval_start_utc_after"] == (
            new_york(after, time(9, 30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    assert [t["date"] for t in manifest()["calendar_definition"]["dst_transitions"]] == [
        "2024-03-10",
        "2024-11-03",
        "2025-03-09",
    ]


def test_generator_new_york_rule_matches_zoneinfo() -> None:
    day = date(2007, 1, 1)
    while day < date(2031, 1, 1):
        if day.weekday() != 6:
            noon = datetime.combine(day, time(12), tzinfo=NEW_YORK)
            assert generator.new_york_utc_offset(day) == noon.utcoffset(), day
        day += timedelta(days=1)


def test_splitmix64_matches_reference_vector() -> None:
    rng = generator.SplitMix64(1234567)
    assert [rng.next_u64() for _ in range(5)] == [
        6457827717110365317,
        3203168211198807973,
        9817491932198370423,
        4593380528125082431,
        16408922859458223821,
    ]


def test_delivery_order_never_moves_availability_backwards() -> None:
    available = [instant(bar["available_at"]) for bar in table(BARS)]
    assert available == sorted(available)


# Validity of every row that no trap declares


def test_ohlc_and_volume_valid_on_non_trap_rows() -> None:
    known = {r["instrument_id"] for r in table(INSTRUMENTS)}
    traps = trap_rows()
    identities = set()
    checked = 0
    for number, bar in enumerate(table(BARS), start=1):
        if number in traps:
            continue
        checked += 1
        assert bar["instrument_id"] in known, number
        assert ohlc_valid(bar), number
        assert int(bar["volume"]) > 0, number
        assert (bar["revision"], bar["final"], bar["adjustment_basis"]) == ("1", "true", "RAW")
        assert instant(bar["available_at"]) == instant(bar["interval_end"]) + PUBLICATION_DELAY
        day = session_of(bar)
        first, last = active_window(bar["instrument_id"])
        assert first <= day <= last, number
        record_id = f"bar-{bar['instrument_id'][-4:]}-{day:%Y%m%d}"
        assert bar["provider_record_id"] == record_id, number
        identities.add((bar["provider_record_id"], bar["revision"]))
    assert checked == len(identities) == len(table(BARS)) - len(traps)
    assert len(traps) == 11


def test_instrument_coverage_has_only_declared_gaps() -> None:
    missing = scenario("missing_session")
    for instrument in (r["instrument_id"] for r in table(INSTRUMENTS)):
        first, last = active_window(instrument)
        expected = {d for d in calendar() if first <= d <= last}
        if instrument == missing["instrument_id"]:
            expected.discard(date.fromisoformat(missing["session_date"]))
        assert {session_of(bar) for bar in bars_of(instrument)} == expected, instrument


def test_price_process_segments_are_visible() -> None:
    """The market proxy basket shows each declared generator segment's character."""
    closes = {session_of(b): float(b["close"]) for b in bars_of("inst_syn_0011")}
    volumes = {session_of(b): int(b["volume"]) for b in bars_of("inst_syn_0011")}
    sessions = calendar()
    stats: dict[str, list[tuple[float, float, float]]] = {}
    for segment in manifest()["process"]["segments"]:
        days = [d for d in sessions if segment["first_session"] <= d.isoformat()]
        days = [d for d in days if d.isoformat() <= segment["last_session"]]
        previous = [closes[d] for d in sessions if d < days[0]] or [closes[days[0]]]
        path = [previous[-1]] + [closes[d] for d in days]
        moves = [math.log(b / a) for a, b in zip(path, path[1:], strict=False)]
        mean_abs = statistics.fmean(abs(m) for m in moves)
        total = math.exp(sum(moves)) - 1
        volume = statistics.fmean(volumes[d] for d in days)
        stats.setdefault(segment["kind"], []).append((mean_abs, total, volume))
    calm = max(mean_abs for mean_abs, _, _ in stats["RANGE_BOUND"])
    assert min(mean_abs for mean_abs, _, _ in stats["HIGH_VOLATILITY"]) > 2 * calm
    assert all(total < -0.08 for _, total, _ in stats["DRAWDOWN"])
    assert all(total > 0 for _, total, _ in stats["TREND"])
    assert all(abs(total) < 0.05 for _, total, _ in stats["RANGE_BOUND"])
    quiet_volume = max(volume for _, _, volume in stats["RANGE_BOUND"])
    assert min(volume for _, _, volume in stats["HIGH_VOLATILITY"]) > quiet_volume


# Declared traps


def test_declared_traps_are_complete_and_covered() -> None:
    scenarios = manifest()["scenarios"]
    assert {s["id"] for s in scenarios} == DECLARED_TRAPS
    assert len(scenarios) == len(DECLARED_TRAPS)
    module = sys.modules[__name__]
    for item in scenarios:
        assert item["covered_by"] == f"tests/test_fixture_dataset.py::test_trap_{item['id']}"
        assert callable(getattr(module, f"test_trap_{item['id']}", None)), item["id"]
        assert item["expected_handling"] and item["expectation"] and item["description"]
        for record in item["records"]:
            declared(item, record["role"])
        record_rows = {r["row"] for r in item["records"] if r["file"] == BARS}
        assert set(item["trap_rows"].get(BARS, [])) <= record_rows


def test_trap_late_revision() -> None:
    item = scenario("late_revision")
    original, revised = declared(item, "revision_1"), declared(item, "revision_2")
    record_id = original["provider_record_id"]
    assert revised["provider_record_id"] == record_id
    assert (original["revision"], revised["revision"]) == ("1", "2")
    assert original["final"] == revised["final"] == "true"
    assert original["interval_start"] == revised["interval_start"]
    assert original["close"] != revised["close"] and original["volume"] != revised["volume"]
    assert ohlc_valid(original) and ohlc_valid(revised)
    published = instant(revised["available_at"])
    assert published > instant(original["available_at"])
    lag = calendar().index(published.astimezone(NEW_YORK).date()) - calendar().index(
        session_of(original)
    )
    assert lag == item["facts"]["revision_lag_sessions"] == 3
    assert latest(record_id, published - timedelta(seconds=1)) == original
    assert latest(record_id, published) == revised
    assert len([bar for bar in table(BARS) if bar["provider_record_id"] == record_id]) == 2


def test_trap_provisional_then_final() -> None:
    item = scenario("provisional_then_final")
    provisional, final = declared(item, "provisional"), declared(item, "final")
    record_id = provisional["provider_record_id"]
    assert final["provider_record_id"] == record_id
    assert (provisional["revision"], provisional["final"]) == ("1", "false")
    assert (final["revision"], final["final"]) == ("2", "true")
    assert ohlc_valid(provisional) and ohlc_valid(final)
    regular_cutoff = instant(provisional["interval_end"]) + PUBLICATION_DELAY
    assert instant(provisional["available_at"]) < regular_cutoff < instant(final["available_at"])
    assert latest(record_id, regular_cutoff) == provisional  # nothing completed is knowable yet
    next_session = date.fromisoformat(item["facts"]["final_published_before_session"])
    assert next_session == calendar()[calendar().index(session_of(provisional)) + 1]
    assert instant(final["available_at"]) < new_york(next_session, time(9, 30))
    assert latest(record_id, instant(final["available_at"])) == final
    assert len(rows_on(item["instrument_id"], session_of(final))) == 2


def test_trap_missing_session() -> None:
    item = scenario("missing_session")
    day = date.fromisoformat(item["session_date"])
    index = calendar().index(day)
    assert rows_on(item["instrument_id"], day) == []
    assert session_of(declared(item, "previous_session")) == calendar()[index - 1]
    assert session_of(declared(item, "next_session")) == calendar()[index + 1]
    assert item["facts"]["previous_session"] == calendar()[index - 1].isoformat()
    assert all(rows_on(other, day) for other in ("inst_syn_0003", "inst_syn_0005"))
    assert item["trap_rows"] == {}


def test_trap_impossible_ohlc() -> None:
    item = scenario("impossible_ohlc")
    bar = declared(item, "impossible")
    assert Decimal(bar["high"]) < Decimal(bar["low"])
    assert not ohlc_valid(bar)
    assert rows_on(item["instrument_id"], date.fromisoformat(item["session_date"])) == [bar]
    assert item["trap_rows"][BARS] == [item["records"][0]["row"]]


def test_trap_identical_duplicate() -> None:
    item = scenario("identical_duplicate")
    first, second = declared(item, "first"), declared(item, "second")
    assert first == second and ohlc_valid(first)
    rows = item["trap_rows"][BARS]
    assert len(rows) == 2 and rows[1] - rows[0] > 1  # redelivered later in the batch
    same = [bar for bar in table(BARS) if bar["provider_record_id"] == first["provider_record_id"]]
    assert same == [first, second]


def test_trap_conflicting_duplicate() -> None:
    item = scenario("conflicting_duplicate")
    first, second = declared(item, "first"), declared(item, "second")
    differing = sorted(k for k in first if first[k] != second[k])
    assert differing == item["facts"]["differing_columns"] == ["close", "volume"]
    assert ohlc_valid(first) and ohlc_valid(second)
    same = [bar for bar in table(BARS) if bar["provider_record_id"] == first["provider_record_id"]]
    assert same == [first, second] and first["revision"] == second["revision"] == "1"


def test_trap_out_of_order_delivery() -> None:
    item = scenario("out_of_order_delivery")
    late, following = (
        declared(item, "delivered_late"),
        declared(item, "next_session_delivered_first"),
    )
    late_row = item["trap_rows"][BARS][0]
    following_row = next(r["row"] for r in item["records"] if r["role"] != "delivered_late")
    assert late_row > following_row  # delivered after the later session's bar
    assert late["instrument_id"] == following["instrument_id"] == item["instrument_id"]
    index = calendar().index(session_of(late))
    assert session_of(following) == calendar()[index + 1]
    assert following["provider_record_id"] != late["provider_record_id"]
    delivered = instant(late["available_at"])
    assert delivered == instant(following["interval_end"]) + timedelta(minutes=20)
    assert delivered > instant(following["available_at"])
    own_cutoff = instant(late["interval_end"]) + PUBLICATION_DELAY
    assert latest(late["provider_record_id"], own_cutoff) is None
    assert latest(late["provider_record_id"], delivered) == late
    assert ohlc_valid(late) and rows_on(item["instrument_id"], session_of(late)) == [late]


def test_trap_unknown_instrument() -> None:
    item = scenario("unknown_instrument")
    bar = declared(item, "unknown_instrument")
    unknown = bar["instrument_id"]
    assert unknown == item["instrument_id"] == "inst_syn_0013"
    assert manifest()["instruments"]["unknown_instrument_ids"] == [unknown]
    for name in (INSTRUMENTS, SYMBOLS, UNIVERSE, ACTIONS):
        assert all(r["instrument_id"] != unknown for r in table(name)), name
    assert bars_of(unknown) == [bar]
    assert ohlc_valid(bar) and bar["final"] == "true"


def test_trap_mid_period_listing() -> None:
    item = scenario("mid_period_listing")
    facts, listed = item["facts"], item["instrument_id"]
    listed_on, notice = (
        date.fromisoformat(facts["listed_on"]),
        instant(facts["notice_available_at"]),
    )
    reference = declared(item, "instrument")
    assert (reference["listed_on"], reference["available_at"]) == (
        facts["listed_on"],
        facts["notice_available_at"],
    )
    symbol, added = declared(item, "listing_symbol"), declared(item, "universe_add")
    assert (symbol["effective_from"], symbol["reason"]) == (facts["listed_on"], "LISTING")
    assert (added["action"], added["reason"]) == ("ADD", "LISTING")
    assert added["effective_from"] == facts["listed_on"]
    assert symbol["available_at"] == added["available_at"] == facts["notice_available_at"]
    first = declared(item, "first_bar")
    assert session_of(first) == listed_on == min(session_of(bar) for bar in bars_of(listed))
    assert notice < instant(first["interval_start"])
    before = calendar()[calendar().index(listed_on) - 1]
    assert listed not in members(before, END_OF_KNOWLEDGE)
    assert listed in members(listed_on, END_OF_KNOWLEDGE)
    assert symbol_of(listed, listed_on, notice - timedelta(seconds=1)) is None
    assert symbol_of(listed, listed_on, notice) == "SYN10"
    remaining = calendar()[calendar().index(listed_on) :]
    assert facts["sessions_listed"] == len(remaining) == len(bars_of(listed))
    assert facts["first_session_with_full_warm_up"] == remaining[199].isoformat()


def test_trap_delisting_retained_in_history() -> None:
    item = scenario("delisting_retained_in_history")
    facts, delisted = item["facts"], item["instrument_id"]
    last_session = date.fromisoformat(facts["last_session"])
    removed_from = date.fromisoformat(facts["removed_from"])
    removal = declared(item, "universe_remove")
    assert (removal["action"], removal["reason"]) == ("REMOVE", "DELISTING")
    assert removal["effective_from"] == facts["removed_from"]
    assert removal["available_at"] == facts["notice_available_at"]
    assert removed_from == calendar()[calendar().index(last_session) + 1]
    assert instant(removal["available_at"]) < new_york(last_session, time(9, 30))
    assert session_of(declared(item, "last_bar")) == last_session
    assert max(session_of(bar) for bar in bars_of(delisted)) == last_session
    history = [d for d in calendar() if d <= last_session]
    assert all(delisted in members(d, END_OF_KNOWLEDGE) for d in history)
    assert delisted not in members(removed_from, END_OF_KNOWLEDGE)
    assert delisted not in members(calendar()[-1], END_OF_KNOWLEDGE)  # survivorship trap
    before_notice = instant(removal["available_at"]) - timedelta(seconds=1)
    assert delisted in members(removed_from, before_notice)  # removal not yet knowable


def test_trap_ticker_rename() -> None:
    item = scenario("ticker_rename")
    facts, renamed = item["facts"], item["instrument_id"]
    old, new = declared(item, "old_symbol"), declared(item, "new_symbol")
    assert (old["symbol"], new["symbol"]) == (facts["old_symbol"], facts["new_symbol"])
    assert old["effective_to"] == new["effective_from"] == facts["effective_from"]
    assert (new["reason"], new["available_at"]) == ("RENAME", facts["notice_available_at"])
    rename_day = date.fromisoformat(facts["effective_from"])
    day_before = calendar()[calendar().index(rename_day) - 1]
    notice = instant(facts["notice_available_at"])
    assert notice < new_york(rename_day, time(9, 30))
    assert symbol_of(renamed, day_before, new_york(day_before, time(16))) == "SYN07"
    assert symbol_of(renamed, rename_day, new_york(rename_day, time(16))) == "SYN7R"
    assert symbol_of(renamed, rename_day, notice - timedelta(seconds=1)) == "SYN07"
    before, after = (
        declared(item, "last_bar_before_rename"),
        declared(item, "first_bar_after_rename"),
    )
    assert (session_of(before), session_of(after)) == (day_before, rename_day)
    assert before["instrument_id"] == after["instrument_id"] == renamed
    periods = table(SYMBOLS)
    for index, first in enumerate(periods):
        for second in periods[index + 1 :]:
            if (
                first["instrument_id"] == second["instrument_id"]
                or first["symbol"] == second["symbol"]
            ):
                assert (second["effective_from"] >= (first["effective_to"] or "9999-12-31")) or (
                    first["effective_from"] >= (second["effective_to"] or "9999-12-31")
                ), (first, second)
    assert [(r["symbol"], r["effective_to"]) for r in periods if r["instrument_id"] == renamed] == [
        ("SYN07", facts["effective_from"]),
        ("SYN7R", ""),
    ]


def test_trap_unsupported_split() -> None:
    item = scenario("unsupported_split")
    facts, split = item["facts"], item["instrument_id"]
    action = declared(item, "split")
    assert (action["action_type"], action["ratio_new"], action["ratio_old"]) == ("SPLIT", "2", "1")
    assert (action["action_id"], action["ex_date"]) == (facts["action_id"], facts["ex_date"])
    assert action["available_at"] == facts["notice_available_at"]
    before, ex_bar = declared(item, "last_bar_before_ex_date"), declared(item, "ex_date_bar")
    ex_date = date.fromisoformat(facts["ex_date"])
    assert session_of(ex_bar) == ex_date
    assert session_of(before) == calendar()[calendar().index(ex_date) - 1]
    assert instant(action["available_at"]) < instant(ex_bar["interval_start"])
    assert (before["close"], ex_bar["open"]) == (
        facts["raw_close_before_ex_date"],
        facts["raw_open_on_ex_date"],
    )
    step = Decimal(ex_bar["open"]) / Decimal(before["close"])
    assert Decimal("0.45") < step < Decimal("0.55")  # a raw 2:1 step, not a real return
    by_day = {session_of(bar): int(bar["volume"]) for bar in bars_of(split)}
    index = calendar().index(ex_date)
    volume_before = statistics.median(by_day[d] for d in calendar()[index - 10 : index])
    volume_after = statistics.median(by_day[d] for d in calendar()[index : index + 10])
    assert volume_after > Decimal("1.5") * Decimal(volume_before)
    assert [r["instrument_id"] for r in table(ACTIONS)] == [split]


def test_trap_stale_tail() -> None:
    item = scenario("stale_tail")
    facts, stale = item["facts"], item["instrument_id"]
    last = date.fromisoformat(facts["last_bar_session"])
    assert session_of(declared(item, "last_bar")) == last
    assert max(session_of(bar) for bar in bars_of(stale)) == last
    trailing = [d for d in calendar() if d > last]
    assert len(trailing) == facts["missing_trailing_sessions"] == 7
    assert calendar()[-1].isoformat() == facts["dataset_last_session"]
    assert stale in members(calendar()[-1], END_OF_KNOWLEDGE)
    assert all(r["instrument_id"] != stale for r in table(ACTIONS))
    assert [r["action"] for r in table(UNIVERSE) if r["instrument_id"] == stale] == ["ADD"]
