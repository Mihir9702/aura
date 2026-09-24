import type { ComponentType } from "react";
import {
  Activity,
  FlaskConical,
  Layers3,
  LayoutDashboard,
  Wallet,
  type LucideIcon,
} from "lucide-react";
import type { Control, EventRecord, Journal, Overview } from "../lib/api";
import { OverviewPage } from "../pages/Overview";
import { PortfolioPage } from "../pages/Portfolio";
import { ResearchPage } from "../pages/Research";
import { StrategyPodsPage } from "../pages/StrategyPods";
import { SystemHealthPage } from "../pages/SystemHealth";

export type PageId =
  "overview" | "portfolio" | "strategy-pods" | "research" | "system-health";

/** Everything the shell hands a page. Each page takes what it needs. */
export type PageProps = {
  data: Overview;
  events: EventRecord[];
  journals: Journal[];
  navigate: (page: PageId) => void;
  openControl: (control: Control) => void;
};

export type PageDefinition = {
  id: PageId;
  /** Sidebar and breadcrumb label. */
  label: string;
  /** Page heading. */
  title: string;
  /** Line under the heading. */
  summary: string;
  icon: LucideIcon;
  component: ComponentType<PageProps>;
};

/** Workspace pages in sidebar order. The first one is the default. */
export const pages: readonly PageDefinition[] = [
  {
    id: "overview",
    label: "Overview",
    title: "The long view.",
    summary: "Every decision starts with evidence. Here’s where things stand.",
    icon: LayoutDashboard,
    component: OverviewPage,
  },
  {
    id: "portfolio",
    label: "Portfolio",
    title: "Portfolio",
    summary: "One Ledger. Every dollar accounted for.",
    icon: Wallet,
    component: PortfolioPage,
  },
  {
    id: "strategy-pods",
    label: "Strategy Pods",
    title: "Strategy Pods",
    summary: "Five methodologies. Independent evidence. One shared portfolio.",
    icon: Layers3,
    component: StrategyPodsPage,
  },
  {
    id: "research",
    label: "Research",
    title: "Research",
    summary: "Build confidence before committing capital.",
    icon: FlaskConical,
    component: ResearchPage,
  },
  {
    id: "system-health",
    label: "System health",
    title: "System health",
    summary: "Clear boundaries. Visible dependencies. Controlled execution.",
    icon: Activity,
    component: SystemHealthPage,
  },
];

/** Looks up a page by id. Unknown ids get the Overview. */
export function findPage(id: string): PageDefinition {
  return pages.find((page) => page.id === id) ?? pages[0];
}
