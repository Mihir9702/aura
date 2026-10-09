import type { components } from "./api.generated";

export type Overview = components["schemas"]["Overview"];
export type Portfolio = components["schemas"]["PortfolioSnapshot"];
export type Strategy = components["schemas"]["Strategy"];
export type ControlName = "entry_halt" | "full_kill";

// These stable display types remain explicit for the ledger-style web interface.
export type Health = {
  database: string;
  execution: string;
  environment: string;
};
export type EventRecord = {
  sequence: number;
  event_id: string;
  event_type: string;
  aggregate_id: string;
  aggregate_version: number;
  correlation_id: string;
  payload: Record<string, unknown>;
  recorded_at: string;
};
export type Journal = {
  id: string;
  source: string;
  facts: Record<string, unknown>;
};

export type Snapshot = {
  overview: Overview;
  events: EventRecord[];
  journals: Journal[];
  health: Health;
};

export class SignInRequired extends Error {}

export const UNREACHABLE =
  "Aura could not reach its API. Start Aura with npm run dev in the project folder, then retry.";

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
  // A proxy with no API behind it answers with an empty or HTML body
  const body = await response.json().catch(() => {
    throw new Error(UNREACHABLE);
  });
  if (response.status === 401) throw new SignInRequired();
  if (!response.ok)
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : "The request could not be validated.",
    );
  return body as T;
}

export async function loadSnapshot(): Promise<Snapshot> {
  const [overview, events, journals, health] = await Promise.all([
    api<Overview>("/overview"),
    api<EventRecord[]>("/events"),
    api<Journal[]>("/journals"),
    api<Health>("/health"),
  ]);
  return { overview, events, journals, health };
}
