import type { components } from "../api.generated";

export type Overview = components["schemas"]["Overview"];
export type EventRecord = {
  event_id: string;
  event_type: string;
  recorded_at: string;
  correlation_id: string;
  payload: Record<string, unknown>;
};
export type Journal = {
  id: string;
  source: string;
  facts: Record<string, unknown>;
};
/** Capability controls the owner can change from System health. */
export type Control = "entry_halt" | "full_kill";

export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch("/api" + path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Aura-Command": "1",
      ...options.headers,
    },
  });
  const body = await response.json().catch(() => {
    throw new Error(
      "Aura could not reach its API. Start Aura with npm run dev in the project folder, then retry.",
    );
  });
  if (!response.ok)
    throw new Error(
      response.status === 401
        ? "SIGN_IN"
        : typeof body.detail === "string"
          ? body.detail
          : "The request could not be validated.",
    );
  return body as T;
}
