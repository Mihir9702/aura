import { describe, expect, it } from "vitest";
import { hashFor, pageFromHash, pages } from "./pages";

describe("page registry", () => {
  it("lists the workspace pages in sidebar order", () => {
    expect(pages.map((page) => page.label)).toEqual([
      "Overview",
      "Portfolio",
      "Strategy Pods",
      "Research",
      "System health",
    ]);
  });
});

describe("hash routes", () => {
  it("gives every page its own hash", () => {
    expect(pages.map((page) => hashFor(page.id))).toEqual([
      "#/overview",
      "#/portfolio",
      "#/strategy-pods",
      "#/research",
      "#/system-health",
    ]);
  });

  it("maps each page's hash back to that page", () => {
    for (const page of pages) expect(pageFromHash(hashFor(page.id))).toBe(page);
    expect(pageFromHash("#/portfolio").label).toBe("Portfolio");
    expect(pageFromHash("#/system-health").label).toBe("System health");
  });

  it("ignores letter case and a trailing slash", () => {
    expect(pageFromHash("#/Strategy-Pods").id).toBe("strategy-pods");
    expect(pageFromHash("#/research/").id).toBe("research");
  });

  it("falls back to the Overview for an empty or unknown hash", () => {
    for (const hash of [
      "",
      "#",
      "#/",
      "#/unknown",
      "#/portfolio/ledger",
      "#/System health",
      "#/constructor",
    ])
      expect(pageFromHash(hash).id, hash).toBe("overview");
  });
});
