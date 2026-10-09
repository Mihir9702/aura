import { describe, expect, it } from "vitest";
import { ROUTES } from "./shell";

describe("the redesigned workspace route registry", () => {
  it("keeps all five sections in the intended order", () => {
    expect(ROUTES.map(([id]) => id)).toEqual([
      "overview", "portfolio", "pods", "research", "operations",
    ]);
  });
  it("has unique route ids and labels", () => {
    expect(new Set(ROUTES.map(([id]) => id)).size).toBe(ROUTES.length);
    expect(new Set(ROUTES.map(([, label]) => label)).size).toBe(ROUTES.length);
  });
  it("has stable shareable hash links", () => {
    expect(ROUTES.map(([id]) => "#/" + id)).toEqual([
      "#/overview", "#/portfolio", "#/pods", "#/research", "#/operations",
    ]);
  });
  it("labels strategy pods and operations separately", () => {
    expect(ROUTES.find(([id]) => id === "pods")?.[1]).toBe("Strategy pods");
    expect(ROUTES.find(([id]) => id === "operations")?.[1]).toBe("Operations");
  });
});
