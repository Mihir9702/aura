import {
  ArrowDownRight,
  ArrowRight,
  Check,
  Layers3,
  Shield,
  Wallet,
} from "lucide-react";
import type { PageProps } from "../app/pages";
import { Badge } from "../components/Badge";
import { money } from "../lib/format";

/** Metrics, capital and readiness. The Overview shows the same summary. */
export function PortfolioSummary({
  data,
  navigate,
}: Pick<PageProps, "data" | "navigate">) {
  return (
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
            No positions yet. Your capital remains in cash while the research
            foundation is prepared.
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
            onClick={() => navigate("system-health")}
          >
            Review dependencies <ArrowRight size={15} />
          </button>
        </section>
      </div>
    </>
  );
}

export function PortfolioPage({ data, journals, navigate }: PageProps) {
  return (
    <>
      <PortfolioSummary data={data} navigate={navigate} />
      <section className="panel">
        <div className="panel-title">
          <h2>Ledger records</h2>
          <Badge>Auditable by design</Badge>
        </div>
        {journals.map((j) => (
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
        ))}
      </section>
    </>
  );
}
