import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  ArrowDownRight,
  ArrowRight,
  BookOpen,
  Check,
  ChevronRight,
  CircleDot,
  Compass,
  FlaskConical,
  Layers3,
  LayoutDashboard,
  LockKeyhole,
  LogOut,
  Radio,
  Shield,
  Wallet,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import "./style.css";
import type { components } from "./api.generated";

type Overview = components["schemas"]["Overview"];
type EventRecord = {
  event_id: string;
  event_type: string;
  recorded_at: string;
  correlation_id: string;
  payload: Record<string, unknown>;
};
type Journal = { id: string; source: string; facts: Record<string, unknown> };
type Page =
  "Overview" | "Portfolio" | "Strategy Pods" | "Research" | "System health";
const money = (value: string | null) =>
  value === null
    ? "Unavailable"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
      }).format(Number(value));

async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch("/api" + path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Aura-Command": "1",
      ...options.headers,
    },
  });
  const body = await response.json().catch(() => {
    throw new Error("Aura could not reach its API. Start Aura with npm run dev in the project folder, then retry.");
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

function Badge({
  children,
  tone = "",
}: {
  children: ReactNode;
  tone?: string;
}) {
  return <span className={"badge " + tone}>{children}</span>;
}
function App() {
  const [data, setData] = useState<Overview | null>(null);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [journals, setJournals] = useState<Journal[]>([]);
  const [page, setPage] = useState<Page>("Overview");
  const [auth, setAuth] = useState(false);
  const [key, setKey] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [updated, setUpdated] = useState<Date | null>(null);
  const [connected, setConnected] = useState(false);
  const [control, setControl] = useState<"entry_halt" | "full_kill" | null>(
    null,
  );
  const [reason, setReason] = useState("");
  const refresh = useCallback(async () => {
    try {
      const [overview, audit, ledger] = await Promise.all([
        api<Overview>("/overview"),
        api<EventRecord[]>("/events"),
        api<Journal[]>("/journals"),
      ]);
      setData(overview);
      setEvents(audit);
      setJournals(ledger);
      setAuth(true);
      setUpdated(new Date());
      setError("");
    } catch (e) {
      const message = (e as Error).message;
      if (message === "SIGN_IN") {
        setAuth(false);
        setData(null);
      } else setError(message);
    }
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  useEffect(() => {
    if (!auth) return;
    const stream = new EventSource("/api/stream");
    stream.onopen = () => setConnected(true);
    stream.onerror = () => setConnected(false);
    stream.addEventListener("refresh", () => void refresh());
    stream.addEventListener("expired", () => {
      stream.close();
      setAuth(false);
      setData(null);
    });
    const poll = setInterval(() => void refresh(), 15000);
    return () => {
      stream.close();
      clearInterval(poll);
    };
  }, [auth, refresh]);
  async function login(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/session", { method: "POST", body: JSON.stringify({ key }) });
      setKey("");
      await refresh();
    } catch (e) {
      setError(
        (e as Error).message === "SIGN_IN"
          ? "That owner key was not accepted."
          : (e as Error).message,
      );
    } finally {
      setBusy(false);
    }
  }
  async function changeControl(e: FormEvent) {
    e.preventDefault();
    if (!control || !data) return;
    setBusy(true);
    try {
      await api("/controls/" + control, {
        method: "POST",
        body: JSON.stringify({
          active: !data.controls[control],
          expected_version: data.controls.version,
          reason,
          command_id: crypto.randomUUID(),
        }),
      });
      setControl(null);
      setReason("");
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const nav: [Page, ReactNode][] = [
    ["Overview", <LayoutDashboard size={18} />],
    ["Portfolio", <Wallet size={18} />],
    ["Strategy Pods", <Layers3 size={18} />],
    ["Research", <FlaskConical size={18} />],
    ["System health", <Activity size={18} />],
  ];
  if (!auth || !data)
    return (
      <main className="login">
        <div className="login-art">
          <div className="brand">
            <span className="brand-mark">a</span> aura
            <span className="brand-dot">.</span>
          </div>
          <div>
            <span className="eyebrow">RESEARCH WITH DISCIPLINE</span>
            <h1>
              A clearer view.
              <br />A considered move.
            </h1>
            <p>
              Evidence first. Measured risk.
              <br />A workspace for the long game.
            </p>
          </div>
          <span className="small">PAPER TRADING ONLY · LOCAL WORKSPACE</span>
        </div>
        <div className="login-side">
          <form onSubmit={login}>
            <LockKeyhole className="login-icon" />
            <h2>Your research starts here.</h2>
            <p>Sign in to your single-owner Aura workspace.</p>
            <label htmlFor="owner-key">Owner key</label>
            <input
              id="owner-key"
              type="password"
              autoComplete="current-password"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              required
            />
            <span className="hint">
              Use AURA_OWNER_KEY from your local .env file, created by the
              bootstrap script.
            </span>
            {error && (
              <p role="alert" className="error">
                {error}
              </p>
            )}
            <button className="primary" disabled={busy}>
              {busy ? "Connecting…" : "Enter workspace"}{" "}
              <ArrowRight size={17} />
            </button>
            <div className="login-note">
              <Shield size={16} /> Observe by default. No real-money execution.
            </div>
          </form>
        </div>
      </main>
    );
  return (
    <div className="app-shell">
      <aside>
        <div className="brand">
          <span className="brand-mark">a</span> aura
          <span className="brand-dot">.</span>
        </div>
        <div className="workspace">
          <span className="workspace-avatar">M</span>
          <div>
            Personal workspace<small>Local · Single owner</small>
          </div>
          <ChevronRight size={14} />
        </div>
        <div className="nav-label">WORKSPACE</div>
        <nav>
          {nav.map(([label, icon]) => (
            <button
              key={label}
              className={page === label ? "selected" : ""}
              onClick={() => setPage(label)}
            >
              {icon}
              {label}
              {page === label && <span className="nav-dot" />}
            </button>
          ))}
        </nav>
        <div className="sidebar-note">
          <div>
            <Compass size={20} />
            <Badge tone="accent">M0 / M1</Badge>
          </div>
          <strong>Built on evidence.</strong>
          <p>
            Your foundation is taking shape. Trading stays off until the
            evidence is ready.
          </p>
          <button onClick={() => setPage("Research")}>
            View readiness <ArrowRight size={14} />
          </button>
        </div>
        <div className="sidebar-bottom">
          <span className="connection-dot" /> Local environment
          <button
            aria-label="Sign out"
            onClick={async () => {
              try {
                await api("/session", { method: "DELETE" });
                setAuth(false);
                setData(null);
              } catch (e) {
                setError((e as Error).message);
              }
            }}
          >
            <LogOut size={16} />
          </button>
        </div>
      </aside>
      <div className="main-shell">
        <header>
          <div className="breadcrumb">
            Workspace <ChevronRight size={13} /> <strong>{page}</strong>
          </div>
          <div className="header-right">
            <Badge tone="accent">PAPER</Badge>
            <span className="header-divider" />
            <span className="owner-avatar">M</span>
          </div>
        </header>
        <main className="content">
          <div className="page-top">
            <div>
              <div className="eyebrow">YOUR PAPER RESEARCH WORKSPACE</div>
              <h1>{page === "Overview" ? "The long view." : page}</h1>
              <p>
                {page === "Overview"
                  ? "Every decision starts with evidence. Here’s where things stand."
                  : page === "Portfolio"
                    ? "One Ledger. Every dollar accounted for."
                    : page === "Strategy Pods"
                      ? "Five methodologies. Independent evidence. One shared portfolio."
                      : page === "Research"
                        ? "Build confidence before committing capital."
                        : "Clear boundaries. Visible dependencies. Controlled execution."}
              </p>
            </div>
            <div className="refresh-status">
              <span
                className={"connection-dot " + (!connected ? "amber" : "")}
              />
              {error
                ? "Data may be stale"
                : connected
                  ? "Workspace connected"
                  : "Reconnecting"}
              <small>
                {updated
                  ? "Updated " +
                    updated.toLocaleTimeString([], {
                      hour: "2-digit",
                      minute: "2-digit",
                    })
                  : "Awaiting data"}
              </small>
            </div>
          </div>
          {error && (
            <div className="error" role="alert">
              {error} <button onClick={() => void refresh()}>Retry</button>
            </div>
          )}
          {data.controls.full_kill && (
            <div className="stop-banner">
              FULL KILL ACTIVE · All new order submissions blocked. No automatic
              liquidation. Human re-arm is required and unavailable until
              dependencies are configured.
            </div>
          )}
          {data.controls.entry_halt && (
            <div className="stop-banner subtle">
              ENTRY HALT ACTIVE · No new or increasing exposure. Reducing exits
              still require permission and healthy dependencies.
            </div>
          )}
          {(page === "Overview" || page === "Portfolio") && (
            <>
              <div className="metrics">
                <section className="metric">
                  <span>
                    Portfolio value <Wallet size={15} />
                  </span>
                  <strong>{money(data.portfolio.equity)}</strong>
                  <small>
                    <span className="connection-dot" />{" "}
                    {data.portfolio.valuation_status === "CASH_ONLY"
                      ? "Cash only · no market exposure"
                      : "Market marks not available"}
                  </small>
                </section>
                <section className="metric">
                  <span>
                    Available cash <ArrowDownRight size={16} />
                  </span>
                  <strong>{money(data.portfolio.cash)}</strong>
                  <small>Shared across all Strategy Pods</small>
                </section>
                <section className="metric">
                  <span>
                    Open positions <Layers3 size={16} />
                  </span>
                  <strong>
                    {data.portfolio.positions.length}
                    <em>positions</em>
                  </strong>
                  <small>No active trading adapter</small>
                </section>
                <section className="metric">
                  <span>
                    Operating mode <Shield size={16} />
                  </span>
                  <strong className="text-metric">Observe</strong>
                  <small>Balanced risk profile · execution off</small>
                </section>
              </div>
              <div className="overview-grid">
                <section className="panel allocation">
                  <div className="panel-title">
                    <h2>Capital, patiently held.</h2>
                    <Badge>USD</Badge>
                  </div>
                  <p>
                    No positions yet. Your capital remains in cash while the
                    research foundation is prepared.
                  </p>
                  <div className="capital-visual">
                    <div className="cash-ring">
                      <span>IN CASH</span>
                      <strong>
                        100<em>%</em>
                      </strong>
                    </div>
                    <div className="allocation-legend">
                      <div>
                        <span className="legend-square" />
                        Cash<strong>{money(data.portfolio.cash)}</strong>
                      </div>
                      <div>
                        <span className="legend-square muted" />
                        Market exposure<strong>$0.00</strong>
                      </div>
                      <div className="allocation-foot">
                        Initial challenge capital
                        <strong>{money(data.portfolio.capital)}</strong>
                      </div>
                    </div>
                  </div>
                  <div className="panel-foot">
                    <Shield size={15} /> Doing nothing is a valid decision.
                  </div>
                </section>
                <section className="panel readiness">
                  <div className="panel-title">
                    <h2>Before the first trade</h2>
                    <span className="small">READINESS</span>
                  </div>
                  <div className="readiness-item">
                    <span className="check-circle">
                      <Check size={15} />
                    </span>
                    <div>
                      <strong>Paper Ledger initialized</strong>
                      <small>One canonical source of portfolio truth</small>
                    </div>
                    <Badge tone="accent">Ready</Badge>
                  </div>
                  {[
                    "Market data provider",
                    "Strategy qualification",
                    "Execution & risk policy",
                  ].map((s, i) => (
                    <div className="readiness-item" key={s}>
                      <span className="step-circle">{i + 2}</span>
                      <div>
                        <strong>{s}</strong>
                        <small>
                          {
                            [
                              "Select and qualify a licensed source",
                              "Validate each Pod and trading horizon",
                              "Approve policies and a paper adapter",
                            ][i]
                          }
                        </small>
                      </div>
                      <Badge>Pending</Badge>
                    </div>
                  ))}
                  <button
                    className="text-button"
                    onClick={() => setPage("System health")}
                  >
                    Review dependencies <ArrowRight size={15} />
                  </button>
                </section>
              </div>
            </>
          )}
          {(page === "Overview" || page === "Strategy Pods") && (
            <section className="panel strategies">
              <div className="panel-title">
                <div>
                  <h2>Strategy Pods</h2>
                  <p>Different perspectives. The same discipline.</p>
                </div>
                <Badge>0 ACTIVE / 5 PLANNED</Badge>
              </div>
              <div className="pod-grid">
                {data.strategies.map((pod, i) => (
                  <div className="pod" key={pod.id}>
                    <div className="pod-head">
                      <span className={"pod-symbol symbol-" + i}>
                        {["↗", "⌁", "✳", "⇄", "∿"][i]}
                      </span>
                      <span className="small">0{i + 1}</span>
                    </div>
                    <h3>{pod.name}</h3>
                    <p>{pod.description}</p>
                    <div>
                      <span className="tiny-dot" />
                      Development
                    </div>
                    <small>Methodology not implemented</small>
                  </div>
                ))}
              </div>
              {page === "Strategy Pods" && (
                <div className="panel-foot">
                  Development → Backtesting → Paper Shadow → Qualified → Active
                  Paper. Code alone never grants allocation authority.
                </div>
              )}
            </section>
          )}
          {(page === "Overview" || page === "Portfolio") && (
            <section className="panel">
              <div className="panel-title">
                <h2>
                  {page === "Portfolio"
                    ? "Ledger records"
                    : "Workspace activity"}
                </h2>
                <Badge>Auditable by design</Badge>
              </div>
              {page === "Portfolio"
                ? journals.map((j) => (
                    <div className="activity-row" key={j.id}>
                      <Wallet size={17} />
                      <div>
                        <strong>
                          {j.facts.kind === "FUNDING"
                            ? "Initial challenge funding"
                            : j.source}
                        </strong>
                        <small>{j.source}</small>
                      </div>
                      <strong>
                        {j.facts.capital
                          ? money(String(j.facts.capital))
                          : "Journal posted"}
                      </strong>
                    </div>
                  ))
                : events
                    .slice(-5)
                    .reverse()
                    .map((event) => (
                      <div className="activity-row" key={event.event_id}>
                        <CircleDot size={16} />
                        <div>
                          <strong>
                            {event.event_type
                              .replaceAll("_", " ")
                              .toLowerCase()}
                          </strong>
                          <small>{event.correlation_id}</small>
                        </div>
                        <time>
                          {new Date(event.recorded_at).toLocaleString()}
                        </time>
                      </div>
                    ))}
            </section>
          )}
          {page === "Research" && (
            <>
              <section className="panel research-intro">
                <FlaskConical size={30} />
                <h2>Hypotheses earn their place.</h2>
                <p>
                  Backtests, prospective Shadow Portfolios, and a multi-metric
                  evidence scorecard will establish what works. None of these
                  research subsystems is implemented yet.
                </p>
                <div className="research-flow">
                  {[
                    "Hypothesis",
                    "Backtest",
                    "Paper Shadow",
                    "Owner review",
                  ].map((s) => (
                    <span key={s}>
                      {s}
                      <ChevronRight size={15} />
                    </span>
                  ))}
                </div>
              </section>
              <div className="three-grid">
                {[
                  [
                    "Research League",
                    "Offline evaluation only. Scores never authorize orders.",
                  ],
                  [
                    "Market Regime",
                    "No canonical regime yet. Deterministic definitions and causal data must be qualified.",
                  ],
                  [
                    "Knowledge Library",
                    "Rights-aware, versioned full-text retrieval. No documents have been imported.",
                  ],
                ].map(([title, text]) => (
                  <section className="panel" key={title}>
                    <BookOpen size={20} />
                    <h2>{title}</h2>
                    <p>{text}</p>
                    <Badge>Not implemented</Badge>
                  </section>
                ))}
              </div>
            </>
          )}
          {page === "System health" && (
            <>
              <section className="panel">
                <div className="panel-title">
                  <h2>Dependencies</h2>
                  <Badge tone="accent">PostgreSQL connected</Badge>
                </div>
                {data.integrations.map((item) => (
                  <div className="activity-row" key={item.name}>
                    <Radio size={17} />
                    <div>
                      <strong>{item.name}</strong>
                      <small>
                        Required integration has not been selected or qualified
                      </small>
                    </div>
                    <Badge>Not configured</Badge>
                  </div>
                ))}
              </section>
              <section className="panel">
                <div className="panel-title">
                  <h2>Capability controls</h2>
                  <Badge>Owner commands · audited</Badge>
                </div>
                <div className="control-row">
                  <div>
                    <h3>Entry Halt</h3>
                    <p>
                      Stop new or increasing exposure. Healthy, authorized
                      reducing exits may continue.
                    </p>
                  </div>
                  <button
                    className="secondary"
                    onClick={() => {
                      setControl("entry_halt");
                      setReason("");
                    }}
                  >
                    {data.controls.entry_halt
                      ? "Release Entry Halt"
                      : "Activate Entry Halt"}
                  </button>
                </div>
                <div className="control-row">
                  <div>
                    <h3>Full Kill</h3>
                    <p>
                      Block all new submissions. Continue receipts and
                      accounting. Never auto-liquidate.
                    </p>
                  </div>
                  <button
                    className="danger"
                    disabled={data.controls.full_kill}
                    onClick={() => {
                      setControl("full_kill");
                      setReason("");
                    }}
                  >
                    {data.controls.full_kill
                      ? "Full Kill active"
                      : "Activate Full Kill"}
                  </button>
                </div>
                <div className="panel-foot">
                  Full Kill re-arm stays unavailable until data, adapter, and
                  reconciliation prerequisites can be verified.
                </div>
              </section>
            </>
          )}
          <footer>
            <span>AURA · A WORKSPACE FOR THE LONG GAME</span>
            <span>Paper only. No real-money execution.</span>
          </footer>
        </main>
      </div>
      {control && (
        <div className="modal-backdrop">
          <form
            className="modal"
            onSubmit={changeControl}
            role="dialog"
            aria-modal="true"
            aria-labelledby="control-title"
          >
            <button
              type="button"
              className="close"
              aria-label="Close control dialog"
              onClick={() => setControl(null)}
            >
              <X size={18} />
            </button>
            <Shield size={26} />
            <h2 id="control-title">
              {control === "full_kill"
                ? "Activate Full Kill"
                : data.controls.entry_halt
                  ? "Release Entry Halt"
                  : "Activate Entry Halt"}
            </h2>
            <p>
              {control === "full_kill"
                ? "This persists across restarts. Re-arm is unavailable until dependency health and reconciliation can be verified. No positions will be liquidated."
                : "This changes the entry capability only. Mode permissions, Full Kill, and component Health Gates remain in effect."}
            </p>
            <label htmlFor="reason">Reason for the audit record</label>
            <input
              id="reason"
              autoFocus
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              minLength={4}
              maxLength={500}
              required
            />
            {error && (
              <p className="error" role="alert">
                {error}
              </p>
            )}
            <button className="primary" disabled={busy}>
              {busy ? "Recording…" : "Confirm control change"}
            </button>
          </form>
        </div>
      )}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
