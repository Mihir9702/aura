// Shown in place of an amount that is missing or not a decimal string.
const UNAVAILABLE = "Unavailable";

// A base-10 decimal string as the API sends it: "500.00000000", "-12.5", or
// the exponent form Python's Decimal uses for small values, such as "0E-8".
const DECIMAL = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/;

// Upper bound on the digits money() will build. Only an absurd exponent,
// such as "1E+999999", comes near it.
const MAX_DIGITS = 1000;

/**
 * Formats a decimal string as US dollars, for example "1234.5" as "$1,234.50".
 *
 * Rounding to cents uses string arithmetic on every digit the server sent.
 * Number() would first cut the amount to about 16 significant digits. The
 * output otherwise matches the Intl.NumberFormat en-US currency format the
 * UI used before: half-away-from-zero rounding, comma grouping, and a leading
 * minus sign, kept even when a negative amount rounds to $0.00.
 */
export function money(value: string | null | undefined): string {
  const amount = value == null ? null : toScaledDigits(value, 2);
  if (amount === null) return UNAVAILABLE;
  const digits = amount.digits.padStart(3, "0");
  const dollars = digits.slice(0, -2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return (amount.negative ? "-$" : "$") + dollars + "." + digits.slice(-2);
}

/**
 * Returns the sign of a decimal string and the digits of its magnitude times
 * 10^places, rounding halves away from zero. "12.345" with 2 places gives
 * "1235". Zero gives "". Returns null when the string is not a decimal.
 */
function toScaledDigits(
  value: string,
  places: number,
): { negative: boolean; digits: string } | null {
  const match = DECIMAL.exec(value);
  if (match === null) return null;
  const [, sign, whole, fraction = "", exponent = "0"] = match;
  if (whole === "" && fraction === "") return null;
  const negative = sign === "-";
  const coefficient = (whole + fraction).replace(/^0+/, "");
  if (coefficient === "") return { negative, digits: "" };
  // The amount is coefficient × 10^-(fraction.length - exponent). Dropping
  // `drop` trailing digits leaves `places` decimal places.
  const drop = fraction.length - Number.parseInt(exponent, 10) - places;
  if (drop <= 0) {
    if (coefficient.length - drop > MAX_DIGITS) return null;
    return { negative, digits: coefficient + "0".repeat(-drop) };
  }
  const keep = coefficient.length - drop;
  if (keep < 0) return { negative, digits: "" };
  const kept = coefficient.slice(0, keep);
  return {
    negative,
    digits:
      coefficient[keep] >= "5" ? (BigInt("0" + kept) + 1n).toString() : kept,
  };
}
