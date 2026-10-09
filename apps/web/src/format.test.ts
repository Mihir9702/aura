import { describe, expect, it } from "vitest";
import { describeEvent, money, postedAt, sentence } from "./format";
import type { EventRecord, Journal } from "./api";

function event(
  event_type: string,
  payload: Record<string, unknown> = {},
  recorded_at = "2026-10-08T12:00:00Z",
): EventRecord {
  return {
    sequence: 1,
    event_id: "test-event",
    event_type,
    aggregate_id: "fixture",
    aggregate_version: 1,
    correlation_id: "test",
    payload,
    recorded_at,
  };
}

describe("current ledger display money formatting", () => {
  it("displays challenge funding and preserves only required cents", () => {
    expect(money("500.00000000")).toBe("$500.00");
    expect(money("500")).toBe("$500.00");
    expect(money("0.00")).toBe("$0.00");
  });

  it("retains sub-cent precision from decimal strings instead of converting to Number", () => {
    expect(money("123456789012.12345678")).toBe("$123,456,789,012.12345678");
    expect(money("0.00000001")).toBe("$0.00000001");
  });

  it("shows negative amounts in accounting parentheses", () => {
    expect(money("-1234.5")).toBe("($1,234.50)");
    expect(money("-0.125")).toBe("($0.125)");
  });

  it("accepts server-supplied whitespace and leading positive signs", () => {
    expect(money(" +1200.50 ")).toBe("$1,200.50");
  });
});

describe("current event and ledger display", () => {
  it("turns stable server codes into readable labels", () => {
    expect(sentence("ACTIVE_PAPER")).toBe("Active paper");
  });
  it("renders an Entry Halt decision with its recorded reason", () => {
    expect(describeEvent(event("ENTRY_HALT_CHANGED", { active: true, reason: "manual pause" })))
      .toEqual({ title: "Entry halt on", detail: "manual pause" });
  });
  it("renders an unknown event safely", () => {
    expect(describeEvent(event("FUTURE_EVENT"))).toEqual({ title: "Future event", detail: "" });
  });
  it("links journal timestamps by journal identity, not arbitrary list position", () => {
    const journal: Journal = { id: "j2", source: "fixture", facts: {} };
    const events = [
      event("LEDGER_POSTED", { journal_id: "j1" }, "earlier"),
      event("LEDGER_POSTED", { journal_id: "j2" }, "correct"),
    ];
    expect(postedAt(journal, events)).toBe("correct");
  });
});
