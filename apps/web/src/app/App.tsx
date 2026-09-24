import {
  ArrowRight,
  ChevronRight,
  Compass,
  LockKeyhole,
  LogOut,
  Shield,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Badge } from "../components/Badge";
import {
  api,
  type Control,
  type EventRecord,
  type Journal,
  type Overview,
} from "../lib/api";
import { findPage, pages, type PageId } from "./pages";

export function App() {
  const [data, setData] = useState<Overview | null>(null);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [journals, setJournals] = useState<Journal[]>([]);
  const [pageId, setPageId] = useState<PageId>("overview");
  const [auth, setAuth] = useState(false);
  const [key, setKey] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [updated, setUpdated] = useState<Date | null>(null);
  const [connected, setConnected] = useState(false);
  const [control, setControl] = useState<Control | null>(null);
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
  function openControl(next: Control) {
    setControl(next);
    setReason("");
  }
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
  const page = findPage(pageId);
  const Page = page.component;
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
          {pages.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              className={page.id === id ? "selected" : ""}
              onClick={() => setPageId(id)}
            >
              <Icon size={18} />
              {label}
              {page.id === id && <span className="nav-dot" />}
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
          <button onClick={() => setPageId("research")}>
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
            Workspace <ChevronRight size={13} /> <strong>{page.label}</strong>
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
              <h1>{page.title}</h1>
              <p>{page.summary}</p>
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
          <Page
            data={data}
            events={events}
            journals={journals}
            navigate={setPageId}
            openControl={openControl}
          />
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
