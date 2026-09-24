import { describe, expect, it } from "vitest";
import { money } from "./format";

// money() as it was before it stopped parsing amounts with Number().
const intl = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});
const previousMoney = (value: string) => intl.format(Number(value));

// Deterministic decimal strings with at most 15 significant digits, the
// range where Number() holds every digit and the old output was exact.
function sampleDecimals(count: number): string[] {
  let seed = 20260923;
  const random = (below: number) => {
    seed = (seed * 48271) % 2147483647;
    return seed % below;
  };
  const samples: string[] = [];
  for (let n = 0; n < count; n++) {
    const wholeDigits = random(13);
    const fractionDigits = random(Math.min(8, 15 - wholeDigits) + 1);
    let digits = "";
    for (let d = 0; d < wholeDigits + fractionDigits; d++) digits += random(10);
    const whole = digits.slice(0, wholeDigits) || "0";
    const fraction = digits.slice(wholeDigits);
    samples.push(
      (random(2) ? "-" : "") + whole + (fraction ? "." + fraction : ""),
    );
  }
  return samples;
}

describe("money", () => {
  it("shows the challenge capital as today", () => {
    expect(money("500.00")).toBe("$500.00");
    expect(money("500.00000000")).toBe("$500.00");
    expect(money("500")).toBe("$500.00");
  });

  it("keeps full precision with 12 integer digits and 8 decimal places", () => {
    // Number() reads the first two as 999999999999.995 and
    // 999999999999.005, so the old output was $1,000,000,000,000.00 and
    // $999,999,999,999.01.
    expect(money("999999999999.99499999")).toBe("$999,999,999,999.99");
    expect(money("999999999999.00499999")).toBe("$999,999,999,999.00");
    expect(money("-999999999999.99499999")).toBe("-$999,999,999,999.99");
    expect(money("123456789012.12345678")).toBe("$123,456,789,012.12");
    expect(money("999999999999.99500000")).toBe("$1,000,000,000,000.00");
  });

  it("formats negatives as today", () => {
    expect(money("-1234.5")).toBe("-$1,234.50");
    expect(money("-0.125")).toBe("-$0.13");
    // Intl keeps the sign when a negative amount rounds to zero.
    expect(money("-0.001")).toBe("-$0.00");
    expect(money("-0")).toBe("-$0.00");
    expect(money("+12")).toBe("$12.00");
  });

  it("rounds half away from zero as today", () => {
    expect(money("0.125")).toBe("$0.13");
    expect(money("0.135")).toBe("$0.14");
    expect(money("0.12499999")).toBe("$0.12");
    expect(money("1.005")).toBe("$1.01");
    expect(money("0.005")).toBe("$0.01");
    expect(money("0.00499999")).toBe("$0.00");
    expect(money("9.995")).toBe("$10.00");
  });

  it("groups thousands as today", () => {
    expect(money("0")).toBe("$0.00");
    expect(money("999.99")).toBe("$999.99");
    expect(money("1000")).toBe("$1,000.00");
    expect(money("999999.995")).toBe("$1,000,000.00");
    expect(money("1234567.891")).toBe("$1,234,567.89");
    expect(money("000123.4")).toBe("$123.40");
  });

  it("reads the exponent form Python's Decimal uses", () => {
    expect(money("0E-8")).toBe("$0.00");
    expect(money("1E-8")).toBe("$0.00");
    expect(money("-1E-8")).toBe("-$0.00");
    expect(money("5E-3")).toBe("$0.01");
    expect(money("5E+2")).toBe("$500.00");
    expect(money("-1.5e3")).toBe("-$1,500.00");
  });

  it("matches the previous output wherever Number() was exact", () => {
    for (const value of sampleDecimals(2000))
      expect(money(value), value).toBe(previousMoney(value));
  });

  it("shows Unavailable for a missing amount", () => {
    expect(money(null)).toBe("Unavailable");
    expect(money(undefined)).toBe("Unavailable");
  });

  it("shows Unavailable for a string that is not a decimal", () => {
    for (const value of [
      "",
      ".",
      "-",
      "abc",
      "NaN",
      "Infinity",
      "0x10",
      "1,000",
      " 12",
      "$5",
      "1e",
      "1E+999999",
    ])
      expect(money(value), value).toBe("Unavailable");
  });
});
