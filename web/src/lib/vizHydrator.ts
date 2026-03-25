/**
 * Article Visualization Hydrator
 *
 * Convention for embedding interactive visualizations in MDX articles:
 *
 *   <div data-viz="TimelineChart" data-src="/content/timelines/apollo.json" data-height="400"></div>
 *   <div data-viz="TransactionTable" data-src="/content/financials/apollo-transactions.json"></div>
 *   <div data-viz="EgoNetwork" data-src="/content/ego/leon-black.json" data-depth="2"></div>
 *
 * The `data-src` attribute points to a JSON file that provides the component props.
 * Optional `data-height` and `data-depth` override props from the JSON.
 *
 * Articles remain readable without JS — the div is empty but the surrounding
 * prose provides context. When JS loads, the script finds all data-viz markers
 * and mounts React components into them.
 */

import { createElement } from 'react';
import { createRoot } from 'react-dom/client';
import TimelineChart from '../components/TimelineChart';
import TransactionTable from '../components/TransactionTable';
import EgoNetwork from '../components/EgoNetwork';
import SankeyDiagram from '../components/SankeyDiagram';
import CorporateStructure from '../components/CorporateStructure';

const COMPONENTS: Record<string, React.ComponentType<any>> = {
  TimelineChart,
  TransactionTable,
  EgoNetwork,
  SankeyDiagram,
  CorporateStructure,
};

/** Components that expect the JSON payload nested under a `data` prop. */
const NESTED_DATA_COMPONENTS = new Set(['SankeyDiagram', 'CorporateStructure']);

export async function hydrateArticleViz() {
  const markers = document.querySelectorAll<HTMLElement>('[data-viz]');
  if (markers.length === 0) return;

  for (const el of markers) {
    const name = el.dataset.viz;
    if (!name || !COMPONENTS[name]) {
      console.warn(`[viz-hydrator] Unknown component: ${name}`);
      continue;
    }

    const src = el.dataset.src;
    if (!src) {
      console.warn(`[viz-hydrator] Missing data-src for ${name}`);
      continue;
    }

    try {
      // Show loading state
      el.innerHTML = '<div style="padding:2rem;text-align:center;color:#8c97a3;font-size:0.85rem;">Loading visualization...</div>';

      const res = await fetch(src);
      if (!res.ok) throw new Error(`Failed to fetch ${src}: ${res.status}`);
      const data = await res.json();

      // Merge data-* overrides
      const overrides: Record<string, any> = {};
      if (el.dataset.height) overrides.height = parseInt(el.dataset.height, 10);
      if (el.dataset.depth) overrides.depth = parseInt(el.dataset.depth, 10);
      if (el.dataset.groupBy) overrides.groupBy = el.dataset.groupBy;
      if (el.dataset.title) overrides.title = el.dataset.title;

      const props = NESTED_DATA_COMPONENTS.has(name!)
        ? { data, ...overrides }
        : { ...data, ...overrides };
      const Component = COMPONENTS[name];
      const root = createRoot(el);
      root.render(createElement(Component, props));
    } catch (err) {
      console.error(`[viz-hydrator] Error mounting ${name}:`, err);
      el.innerHTML = `<div style="padding:1rem;color:#b7b1a3;font-size:0.85rem;border:1px solid #2a313b;border-radius:4px;">Visualization unavailable: ${name}</div>`;
    }
  }
}
