import type { ReactNode } from "react";
import type { PageProps } from "../app/pages";
import { Badge } from "../components/Badge";
import type { Overview } from "../lib/api";

/** The Pod registry. The Overview shows the same panel without a footer. */
export function StrategyPodsPanel({
  strategies,
  children,
}: {
  strategies: Overview["strategies"];
  children?: ReactNode;
}) {
  return (
    <section className="panel strategies">
      <div className="panel-title">
        <div>
          <h2>Strategy Pods</h2>
          <p>Different perspectives. The same discipline.</p>
        </div>
        <Badge>0 ACTIVE / 5 PLANNED</Badge>
      </div>
      <div className="pod-grid">
        {strategies.map((pod, i) => (
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
      {children}
    </section>
  );
}

export function StrategyPodsPage({ data }: PageProps) {
  return (
    <StrategyPodsPanel strategies={data.strategies}>
      <div className="panel-foot">
        Development → Backtesting → Paper Shadow → Qualified → Active Paper.
        Code alone never grants allocation authority.
      </div>
    </StrategyPodsPanel>
  );
}
