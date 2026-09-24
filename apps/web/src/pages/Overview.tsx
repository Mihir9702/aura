import { CircleDot } from "lucide-react";
import type { PageProps } from "../app/pages";
import { Badge } from "../components/Badge";
import { PortfolioSummary } from "./Portfolio";
import { StrategyPodsPanel } from "./StrategyPods";

export function OverviewPage({ data, events, navigate }: PageProps) {
  return (
    <>
      <PortfolioSummary data={data} navigate={navigate} />
      <StrategyPodsPanel strategies={data.strategies} />
      <section className="panel">
        <div className="panel-title">
          <h2>Workspace activity</h2>
          <Badge>Auditable by design</Badge>
        </div>
        {events
          .slice(-5)
          .reverse()
          .map((event) => (
            <div className="activity-row" key={event.event_id}>
              <CircleDot size={16} />
              <div>
                <strong>
                  {event.event_type.replaceAll("_", " ").toLowerCase()}
                </strong>
                <small>{event.correlation_id}</small>
              </div>
              <time>{new Date(event.recorded_at).toLocaleString()}</time>
            </div>
          ))}
      </section>
    </>
  );
}
