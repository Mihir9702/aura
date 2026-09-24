import { Radio } from "lucide-react";
import type { PageProps } from "../app/pages";
import { Badge } from "../components/Badge";

export function SystemHealthPage({ data, openControl }: PageProps) {
  return (
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
              Stop new or increasing exposure. Healthy, authorized reducing
              exits may continue.
            </p>
          </div>
          <button
            className="secondary"
            onClick={() => openControl("entry_halt")}
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
              Block all new submissions. Continue receipts and accounting. Never
              auto-liquidate.
            </p>
          </div>
          <button
            className="danger"
            disabled={data.controls.full_kill}
            onClick={() => openControl("full_kill")}
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
  );
}
