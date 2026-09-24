import { BookOpen, ChevronRight, FlaskConical } from "lucide-react";
import { Badge } from "../components/Badge";

export function ResearchPage() {
  return (
    <>
      <section className="panel research-intro">
        <FlaskConical size={30} />
        <h2>Hypotheses earn their place.</h2>
        <p>
          Backtests, prospective Shadow Portfolios, and a multi-metric evidence
          scorecard will establish what works. None of these research subsystems
          is implemented yet.
        </p>
        <div className="research-flow">
          {["Hypothesis", "Backtest", "Paper Shadow", "Owner review"].map(
            (s) => (
              <span key={s}>
                {s}
                <ChevronRight size={15} />
              </span>
            ),
          )}
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
  );
}
