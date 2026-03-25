import { useEffect, useRef, useState, useMemo } from 'react';
import * as d3 from 'd3';
import { graphStratify, sugiyama, layeringSimplex, coordSimplex } from 'd3-dag';

// --- Interfaces ---

interface StructureNode {
  id: string;
  name: string;
  nodeType: 'person' | 'entity';
  entityType?: string;
  jurisdiction?: string;
  status?: string;
  role?: string;
  emphasis?: 'primary' | 'secondary';
  parentIds: string[];
  x?: number;
  y?: number;
}

interface StructureEdge {
  source: string;
  target: string;
  relationType: string;
  description?: string;
  label?: string;
  category?: string;
  evidence?: 'confirmed' | 'inferred' | 'suspected';
  strength?: number;
  showLabel?: boolean;
}

interface StructureData {
  id: string;
  title: string;
  subtitle?: string;
  nodes: StructureNode[];
  edges: StructureEdge[];
  annotations?: StructureAnnotation[];
}

interface Props {
  data: StructureData;
  height?: number;
}

interface StructureAnnotation {
  id: string;
  targetId: string;
  text: string;
  align?: 'left' | 'right' | 'top' | 'bottom';
  dx?: number;
  dy?: number;
}

// --- Design tokens (mirroring CSS custom properties for SVG use) ---

const C = {
  void: '#0b0d10',
  stone: '#12151b',
  slate: '#1c222b',
  ash: '#2a313b',
  mithril: '#8c97a3',
  moonlight: '#c7d0d9',
  icy: '#8fd3e8',
  ember: '#d1b36a',
} as const;

const FONT = {
  ui: '"Space Grotesk", sans-serif',
  mono: '"IBM Plex Mono", monospace',
} as const;

// --- Colors by jurisdiction ---

const DEFAULT_JURISDICTION_COLORS: Record<string, string> = {
  USVI: C.ember,
  NY: C.icy,
  DE: '#7ea7c1',
  FL: '#8fa6b8',
  NM: '#9aa6b2',
  OH: C.mithril,
  CA: '#9aa6b2',
  NJ: '#9aa6b2',
  UK: '#b7b1a3',
  'UNITED KINGDOM': '#b7b1a3',
  'CAYMAN ISLANDS': '#a09c8a',
  CAYMAN: '#a09c8a',
  BVI: '#8fa6b8',
  'BRITISH VIRGIN ISLANDS': '#8fa6b8',
  PANAMA: '#b7b1a3',
  JERSEY: '#a09c8a',
  LUXEMBOURG: '#7ea7c1',
  IRELAND: '#8fa6b8',
  NETHERLANDS: '#7ea7c1',
  MAURITIUS: '#a09c8a',
  SINGAPORE: '#8fa6b8',
  'HONG KONG': '#7ea7c1',
  SWITZERLAND: '#b7b1a3',
};

const FALLBACK_JURISDICTION_PALETTE = [
  '#7ea7c1',
  '#8fa6b8',
  '#9aa6b2',
  '#b7b1a3',
  '#a09c8a',
  '#6f8796',
];

const RELATIONSHIP_COLORS: Record<string, string> = {
  ownership: C.moonlight,
  financial: C.ember,
  trust: C.icy,
  governance: C.mithril,
  corporate: '#7ea7c1',
  legal: '#b7b1a3',
  social: C.icy,
  intelligence: '#a09c8a',
  advisory: '#9aa6b2',
  other: C.mithril,
};

const EVIDENCE_DASH: Record<'confirmed' | 'inferred' | 'suspected', string> = {
  confirmed: '',
  inferred: '6 4',
  suspected: '2 4',
};

const RELATIONSHIP_DASH: Record<string, string> = {
  ownership: '',
  financial: '',
  trust: '',
  governance: '',
  other: '',
};

function normalizeJurisdiction(jurisdiction: string): string {
  return jurisdiction.trim().toUpperCase();
}

function buildJurisdictionColorMap(nodes: StructureNode[]): Map<string, string> {
  const map = new Map<string, string>();
  let paletteIndex = 0;
  for (const node of nodes) {
    if (!node.jurisdiction) continue;
    if (map.has(node.jurisdiction)) continue;
    const key = normalizeJurisdiction(node.jurisdiction);
    const known = DEFAULT_JURISDICTION_COLORS[key];
    if (known) {
      map.set(node.jurisdiction, known);
      continue;
    }
    const color = FALLBACK_JURISDICTION_PALETTE[paletteIndex % FALLBACK_JURISDICTION_PALETTE.length];
    paletteIndex += 1;
    map.set(node.jurisdiction, color);
  }
  return map;
}

function nodeStrokeColor(node: StructureNode, jurisdictionColorMap: Map<string, string>): string {
  if (node.nodeType === 'person') return C.icy;
  if (node.jurisdiction && jurisdictionColorMap.get(node.jurisdiction)) {
    return jurisdictionColorMap.get(node.jurisdiction)!;
  }
  return C.mithril;
}

// --- Edge styles ---

type StrokeStyle = { dash: string; width: number; color: string; opacity: number };

function inferEdgeCategory(edge: StructureEdge): string {
  if (edge.category) return edge.category;
  const rt = edge.relationType.toLowerCase();
  if (['owns', 'controls', 'ownership', 'subsidiary', 'parent', 'holding', 'holdco'].some(k => rt.includes(k))) {
    return 'ownership';
  }
  if (['fund', 'funds', 'loan', 'fee', 'royalty', 'dividend', 'payment', 'invest', 'distribution', 'return', 'management'].some(k => rt.includes(k))) {
    return 'financial';
  }
  if (['trustee', 'beneficiary', 'settlor', 'protector'].some(k => rt.includes(k))) {
    return 'trust';
  }
  if (['officer', 'director', 'manager', 'advisor', 'agent', 'member', 'partner', 'founder'].some(k => rt.includes(k))) {
    return 'governance';
  }
  return 'other';
}

function edgeStyle(edge: StructureEdge): StrokeStyle {
  const category = inferEdgeCategory(edge);
  const color = RELATIONSHIP_COLORS[category] || RELATIONSHIP_COLORS.other;
  const strength = Math.max(0, Math.min(1, edge.strength ?? 0.5));
  const baseWidth = category === 'ownership' ? 2.2 : 1.3;
  const width = baseWidth + strength * 1.4;
  const dash = edge.evidence ? EVIDENCE_DASH[edge.evidence] : (RELATIONSHIP_DASH[category] || '');
  const opacity = category === 'ownership' ? 0.9 : 0.65;
  return { dash, width, color, opacity };
}

function edgeDisplayLabel(edge: StructureEdge, showAll: boolean): string | null {
  if (edge.showLabel === false) return null;
  if (edge.label) return edge.label;
  if (edge.showLabel || showAll) return edge.relationType;
  return null;
}

// --- Node dimensions ---

function wrapLabel(text: string, maxChars = 18, maxLines = 2): string[] {
  const clean = text.trim();
  if (clean.length <= maxChars) return [clean];
  const words = clean.split(/\s+/);
  const lines: string[] = [];
  let current = '';
  for (const word of words) {
    const next = current ? `${current} ${word}` : word;
    if (next.length <= maxChars || current.length === 0) {
      current = next;
    } else {
      lines.push(current);
      current = word;
      if (lines.length >= maxLines - 1) break;
    }
  }
  if (current) lines.push(current);
  if (lines.length > maxLines) lines.length = maxLines;
  if (lines.length === maxLines && words.join(' ').length > lines.join(' ').length) {
    lines[maxLines - 1] = lines[maxLines - 1].slice(0, Math.max(0, maxChars - 1)) + '\u2026';
  }
  return lines;
}

function nodeLabelLines(name: string): string[] {
  return wrapLabel(name, 18, 2);
}

function nodeSubLabel(node: StructureNode): string | null {
  if (node.nodeType !== 'entity') return null;
  const roleOrType = node.role || node.entityType?.toUpperCase();
  const parts = [roleOrType, node.jurisdiction].filter(Boolean);
  if (!parts.length) return null;
  const label = parts.join(' \u00b7 ');
  return label.length > 28 ? `${label.slice(0, 26)}\u2026` : label;
}

function nodeBoxHeight(node: StructureNode): number {
  const lines = nodeLabelLines(node.name);
  const base = node.nodeType === 'person' ? 34 : 38;
  return base + (lines.length - 1) * 12;
}

function nodeLayoutHeight(node: StructureNode): number {
  const base = nodeBoxHeight(node);
  return base + (nodeSubLabel(node) ? 14 : 0);
}

function nodeWidth(node: StructureNode): number {
  const lines = nodeLabelLines(node.name);
  const maxLen = Math.max(...lines.map(l => l.length));
  return Math.max(140, Math.min(220, maxLen * 7.2 + 36));
}

type ShapeKind = 'person' | 'trust' | 'foundation' | 'fund' | 'holding' | 'company' | 'other';

function nodeShapeKind(node: StructureNode): ShapeKind {
  if (node.nodeType === 'person') return 'person';
  const et = (node.entityType || '').toLowerCase();
  if (et.includes('trust')) return 'trust';
  if (['foundation', 'nonprofit'].some(k => et.includes(k))) return 'foundation';
  if (['fund', 'partnership'].some(k => et.includes(k))) return 'fund';
  if (['spv', 'holding', 'holdco', 'holding company', 'top holdco'].some(k => et.includes(k))) return 'holding';
  if (['llc', 'inc', 'ltd', 'company', 'corp'].some(k => et.includes(k))) return 'company';
  return 'other';
}

/** Draw shape at (0,0) center. */
function drawNodeShape(
  g: d3.Selection<SVGGElement, unknown, null, undefined>,
  node: StructureNode,
  w: number,
  h: number,
  jurisdictionColorMap: Map<string, string>,
) {
  const stroke = nodeStrokeColor(node, jurisdictionColorMap);
  const dissolved = node.status === 'dissolved';
  const opacity = dissolved ? 0.35 : 0.96;
  const strokeDash = dissolved ? '4 3' : '';
  const shape = nodeShapeKind(node);
  const strokeWidth = node.emphasis === 'primary' ? 1.8 : 1.1;
  const fill = C.stone;

  if (shape === 'person') {
    g.append('ellipse')
      .attr('rx', w / 2).attr('ry', h / 2)
      .attr('fill', fill).attr('fill-opacity', opacity)
      .attr('stroke', stroke).attr('stroke-width', strokeWidth)
      .attr('stroke-dasharray', strokeDash);
  } else if (shape === 'trust') {
    const pts = [
      [0, -h / 2], [w / 2, -h / 6], [w / 2, h / 2], [-w / 2, h / 2], [-w / 2, -h / 6],
    ];
    g.append('polygon')
      .attr('points', pts.map(p => p.join(',')).join(' '))
      .attr('fill', fill).attr('fill-opacity', opacity)
      .attr('stroke', stroke).attr('stroke-width', strokeWidth)
      .attr('stroke-dasharray', strokeDash);
  } else if (shape === 'foundation') {
    g.append('rect')
      .attr('x', -w / 2).attr('y', -h / 2).attr('width', w).attr('height', h)
      .attr('rx', 12)
      .attr('fill', fill).attr('fill-opacity', opacity)
      .attr('stroke', stroke).attr('stroke-width', strokeWidth)
      .attr('stroke-dasharray', strokeDash);
  } else if (shape === 'fund') {
    const skew = 10;
    const pts = [
      [-w / 2 + skew, -h / 2], [w / 2 + skew, -h / 2],
      [w / 2 - skew, h / 2], [-w / 2 - skew, h / 2],
    ];
    g.append('polygon')
      .attr('points', pts.map(p => p.join(',')).join(' '))
      .attr('fill', fill).attr('fill-opacity', opacity)
      .attr('stroke', stroke).attr('stroke-width', strokeWidth)
      .attr('stroke-dasharray', strokeDash);
  } else if (shape === 'holding') {
    const inset = w * 0.18;
    const pts = [
      [-w / 2 + inset, -h / 2],
      [w / 2 - inset, -h / 2],
      [w / 2, 0],
      [w / 2 - inset, h / 2],
      [-w / 2 + inset, h / 2],
      [-w / 2, 0],
    ];
    g.append('polygon')
      .attr('points', pts.map(p => p.join(',')).join(' '))
      .attr('fill', fill).attr('fill-opacity', opacity)
      .attr('stroke', stroke).attr('stroke-width', strokeWidth)
      .attr('stroke-dasharray', strokeDash);
  } else if (shape === 'company') {
    g.append('rect')
      .attr('x', -w / 2).attr('y', -h / 2).attr('width', w).attr('height', h)
      .attr('rx', 3)
      .attr('fill', fill).attr('fill-opacity', opacity)
      .attr('stroke', stroke).attr('stroke-width', strokeWidth)
      .attr('stroke-dasharray', strokeDash);
  } else {
    g.append('rect')
      .attr('x', -w / 2).attr('y', -h / 2).attr('width', w).attr('height', h)
      .attr('rx', 3)
      .attr('fill', fill).attr('fill-opacity', opacity)
      .attr('stroke', stroke).attr('stroke-width', strokeWidth)
      .attr('stroke-dasharray', '4 3');
  }
}

// --- Legend data ---

const SHAPE_ENTRIES: Array<{ label: string; kind: ShapeKind }> = [
  { label: 'Person', kind: 'person' },
  { label: 'LLC / Inc', kind: 'company' },
  { label: 'Trust', kind: 'trust' },
  { label: 'Foundation', kind: 'foundation' },
  { label: 'Fund / Partnership', kind: 'fund' },
  { label: 'Holding / SPV', kind: 'holding' },
];

const RELATIONSHIP_LEGEND: Array<{ category: string; label: string }> = [
  { category: 'ownership', label: 'Owns / Controls' },
  { category: 'financial', label: 'Funds / Fees' },
  { category: 'trust', label: 'Trustee / Beneficiary' },
  { category: 'governance', label: 'Officer / Director' },
  { category: 'other', label: 'Other' },
];

// --- Component ---

export default function CorporateStructure({ data, height = 600 }: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [containerWidth, setContainerWidth] = useState(0);
  const [collapsedSet, setCollapsedSet] = useState<Set<string>>(new Set());
  const [highlightedIds, setHighlightedIds] = useState<Set<string> | null>(null);

  // Responsive width
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const update = () => {
      const rect = container.getBoundingClientRect();
      if (rect.width) setContainerWidth(Math.floor(rect.width));
    };
    update();
    if (typeof ResizeObserver !== 'undefined') {
      const obs = new ResizeObserver(update);
      obs.observe(container);
      return () => obs.disconnect();
    }
    window.addEventListener('resize', update);
    return () => window.removeEventListener('resize', update);
  }, []);

  // Compute visible nodes/edges after collapsing subtrees
  const { visibleNodes, visibleEdges, childCounts } = useMemo(() => {
    const childrenOf = new Map<string, string[]>();
    for (const n of data.nodes) {
      for (const pid of n.parentIds) {
        if (!childrenOf.has(pid)) childrenOf.set(pid, []);
        childrenOf.get(pid)!.push(n.id);
      }
    }

    const hidden = new Set<string>();
    for (const cid of collapsedSet) {
      const queue = [...(childrenOf.get(cid) || [])];
      for (const q of queue) {
        if (!hidden.has(q)) {
          hidden.add(q);
          for (const ch of childrenOf.get(q) || []) queue.push(ch);
        }
      }
    }

    const counts = new Map<string, number>();
    for (const [pid, ch] of childrenOf.entries()) {
      counts.set(pid, ch.length);
    }

    const vNodes = data.nodes.filter(n => !hidden.has(n.id));
    const nodeIds = new Set(vNodes.map(n => n.id));
    const filteredNodes = vNodes.map(n => ({
      ...n,
      parentIds: n.parentIds.filter(pid => nodeIds.has(pid)),
    }));
    const vEdges = data.edges.filter(e => nodeIds.has(e.source) && nodeIds.has(e.target));

    return { visibleNodes: filteredNodes, visibleEdges: vEdges, childCounts: counts };
  }, [data, collapsedSet]);

  const jurisdictionColorMap = useMemo(() => buildJurisdictionColorMap(visibleNodes), [visibleNodes]);

  const nodeById = useMemo(() => {
    const map = new Map<string, StructureNode>();
    for (const node of visibleNodes) map.set(node.id, node);
    return map;
  }, [visibleNodes]);

  const shapeLegendEntries = useMemo(() => {
    const kinds = new Set<ShapeKind>();
    for (const node of visibleNodes) kinds.add(nodeShapeKind(node));
    return SHAPE_ENTRIES.filter(entry => kinds.has(entry.kind));
  }, [visibleNodes]);

  const jurisdictionEntries = useMemo(() => {
    return Array.from(jurisdictionColorMap.entries())
      .map(([label, color]) => ({ label, color }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [jurisdictionColorMap]);

  const relationshipLegendEntries = useMemo(() => {
    const cats = new Set<string>();
    for (const edge of visibleEdges) cats.add(inferEdgeCategory(edge));
    return RELATIONSHIP_LEGEND.filter(entry => cats.has(entry.category));
  }, [visibleEdges]);

  const evidenceLegendEntries = useMemo(() => {
    if (!visibleEdges.some(edge => edge.evidence)) return [];
    return [
      { label: 'Confirmed', dash: EVIDENCE_DASH.confirmed },
      { label: 'Inferred', dash: EVIDENCE_DASH.inferred },
      { label: 'Suspected', dash: EVIDENCE_DASH.suspected },
    ];
  }, [visibleEdges]);

  const edgesForRender = useMemo(() => {
    const withIndex = visibleEdges.map((edge, idx) => ({ ...edge, _index: idx }));
    const groups = new Map<string, typeof withIndex>();
    for (const edge of withIndex) {
      const key = `${edge.source}|${edge.target}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push(edge);
    }
    const enriched: Array<StructureEdge & { _index: number; offset: number; category: string; style: StrokeStyle; priority: number }> = [];
    for (const group of groups.values()) {
      const total = group.length;
      const start = -((total - 1) / 2) * 6;
      group.forEach((edge, i) => {
        const category = inferEdgeCategory(edge);
        const style = edgeStyle(edge);
        const priority = category === 'ownership' ? 0 : category === 'trust' ? 1 : category === 'financial' ? 2 : 3;
        enriched.push({ ...edge, _index: edge._index, offset: start + i * 6, category, style, priority });
      });
    }
    return enriched.sort((a, b) => a.priority - b.priority || a._index - b._index);
  }, [visibleEdges]);

  // Ancestor/descendant maps for click highlighting
  const { ancestors, descendants } = useMemo(() => {
    const anc = new Map<string, Set<string>>();
    const desc = new Map<string, Set<string>>();
    const parentMap = new Map<string, string[]>();
    const childMap = new Map<string, string[]>();
    for (const n of visibleNodes) {
      parentMap.set(n.id, n.parentIds);
      for (const pid of n.parentIds) {
        if (!childMap.has(pid)) childMap.set(pid, []);
        childMap.get(pid)!.push(n.id);
      }
    }

    function getAncestors(id: string, visited: Set<string>): Set<string> {
      if (anc.has(id)) return anc.get(id)!;
      const result = new Set<string>();
      for (const pid of parentMap.get(id) || []) {
        if (visited.has(pid)) continue;
        visited.add(pid);
        result.add(pid);
        for (const a of getAncestors(pid, visited)) result.add(a);
      }
      anc.set(id, result);
      return result;
    }

    function getDescendants(id: string, visited: Set<string>): Set<string> {
      if (desc.has(id)) return desc.get(id)!;
      const result = new Set<string>();
      for (const cid of childMap.get(id) || []) {
        if (visited.has(cid)) continue;
        visited.add(cid);
        result.add(cid);
        for (const d of getDescendants(cid, visited)) result.add(d);
      }
      desc.set(id, result);
      return result;
    }

    for (const n of visibleNodes) {
      getAncestors(n.id, new Set([n.id]));
      getDescendants(n.id, new Set([n.id]));
    }

    return { ancestors: anc, descendants: desc };
  }, [visibleNodes]);

  // Main render
  useEffect(() => {
    if (!svgRef.current || visibleNodes.length === 0 || !containerWidth) return;

    // Build DAG
    let dag;
    try {
      const builder = graphStratify()
        .id((d: StructureNode) => d.id)
        .parentIds((d: StructureNode) => d.parentIds);
      dag = builder(visibleNodes);
    } catch (err) {
      console.error('[CorporateStructure] graphStratify failed:', err);
      return;
    }

    // Layout
    const layout = sugiyama()
      .nodeSize((node: any) => {
        const d: StructureNode = node.data;
        return [nodeWidth(d) + 40, nodeLayoutHeight(d) + 60];
      })
      .gap([40, 60])
      .layering(layeringSimplex())
      .coord(coordSimplex());

    let layoutW: number, layoutH: number;
    try {
      const result = layout(dag);
      layoutW = result.width;
      layoutH = result.height;
    } catch (err) {
      console.error('[CorporateStructure] sugiyama layout failed:', err);
      return;
    }

    // Build a position map from dag nodes for reliable edge drawing
    const posMap = new Map<string, { x: number; y: number }>();
    for (const dagNode of dag.nodes()) {
      posMap.set(dagNode.data.id, { x: dagNode.x!, y: dagNode.y! });
    }

    // Viewport: fit content width to container, derive height proportionally
    const pad = 40;
    const totalW = layoutW + pad * 2;
    const totalH = layoutH + pad * 2;

    // Scale so layout width fills container, then compute pixel height from that
    const fitScale = containerWidth / totalW;
    const svgH = Math.max(300, Math.min(height, Math.ceil(totalH * fitScale) + 10));

    const svgEl = d3.select(svgRef.current);
    svgEl.selectAll('*').remove();
    svgEl
      .attr('width', containerWidth)
      .attr('height', svgH);

    // Zoom group
    const g = svgEl.append('g');

    const zoom = d3.zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.15, 4])
      .filter((event: any) => {
        if (event.type === 'wheel') return event.ctrlKey || event.metaKey;
        if (event.type === 'dblclick') return false;
        return !event.button;
      })
      .on('zoom', (event) => g.attr('transform', event.transform));
    svgEl.call(zoom);

    // Initial transform: center content in SVG pixel space
    const tx = (containerWidth - totalW * fitScale) / 2;
    const ty = (svgH - totalH * fitScale) / 2;
    svgEl.call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(fitScale));

    // Arrow marker defs (dynamic per edge color)
    const defs = svgEl.append('defs');
    const markerColors = Array.from(new Set(edgesForRender.map(edge => edge.style.color)));
    const markerMap = new Map<string, string>();
    markerColors.forEach((color, i) => {
      const id = `arr-${i}`;
      markerMap.set(color, id);
      defs.append('marker')
        .attr('id', id)
        .attr('viewBox', '0 0 8 6')
        .attr('refX', 7).attr('refY', 3)
        .attr('markerWidth', 7).attr('markerHeight', 6)
        .attr('orient', 'auto')
        .append('path')
        .attr('d', 'M0,0.4 L7,3 L0,5.6 Z')
        .attr('fill', color);
    });

    function markerFor(color: string) {
      const id = markerMap.get(color);
      return id ? `url(#${id})` : '';
    }

    // Tooltip
    const tooltip = d3.select('body')
      .append('div')
      .style('position', 'absolute')
      .style('visibility', 'hidden')
      .style('background', C.stone)
      .style('color', C.moonlight)
      .style('border', `1px solid ${C.ash}`)
      .style('padding', '8px 12px')
      .style('border-radius', '4px')
      .style('font-family', FONT.ui)
      .style('font-size', '12px')
      .style('line-height', '1.5')
      .style('max-width', '300px')
      .style('pointer-events', 'none')
      .style('z-index', '1000')
      .style('box-shadow', '0 6px 18px rgba(0,0,0,0.45)');

    // --- Draw edges using node positions directly ---
    const edgeGroup = g.append('g');
    const edgeLabelGroup = g.append('g');

    for (const edge of edgesForRender) {
      const sourceId = edge.source;
      const targetId = edge.target;
      const style = edge.style;

      const src = posMap.get(sourceId);
      const tgt = posMap.get(targetId);
      const srcNode = nodeById.get(sourceId);
      const tgtNode = nodeById.get(targetId);
      if (!src || !tgt || !srcNode || !tgtNode) continue;

      const srcH = nodeBoxHeight(srcNode) / 2;
      const tgtH = nodeBoxHeight(tgtNode) / 2;

      const upward = src.y > tgt.y;
      const x1 = src.x + pad + edge.offset;
      const x2 = tgt.x + pad + edge.offset;
      const y1 = src.y + (upward ? -srcH : srcH) + pad;
      const y2 = tgt.y + (upward ? tgtH : -tgtH) + pad;

      const dimmed = highlightedIds && (!highlightedIds.has(sourceId) || !highlightedIds.has(targetId));

      const midY = (y1 + y2) / 2;
      const pathD = `M${x1},${y1} V${midY} H${x2} V${y2}`;

      edgeGroup.append('path')
        .attr('d', pathD)
        .attr('fill', 'none')
        .attr('stroke', style.color)
        .attr('stroke-width', style.width)
        .attr('stroke-dasharray', style.dash || 'none')
        .attr('stroke-opacity', dimmed ? 0.08 : style.opacity)
        .attr('marker-end', markerFor(style.color))
        .style('transition', 'stroke-opacity 0.2s ease')
        .on('mouseover', function () {
          d3.select(this).attr('stroke-opacity', 1).attr('stroke-width', style.width + 1);
          const label = edge.label || edge.relationType;
          const desc = edge.description ? `<div style="margin-top:4px;color:${C.mithril};font-size:11px">${edge.description}</div>` : '';
          const evidence = edge.evidence ? `<div style="margin-top:4px;color:${C.mithril};font-size:11px;text-transform:uppercase;letter-spacing:0.08em">${edge.evidence}</div>` : '';
          tooltip.style('visibility', 'visible')
            .html(`<span style="color:${style.color};font-weight:600">${label}</span>${desc}${evidence}`);
        })
        .on('mousemove', (event: any) => {
          tooltip.style('top', (event.pageY - 10) + 'px').style('left', (event.pageX + 14) + 'px');
        })
        .on('mouseout', function () {
          d3.select(this).attr('stroke-opacity', dimmed ? 0.08 : style.opacity).attr('stroke-width', style.width);
          tooltip.style('visibility', 'hidden');
        });

      const labelText = edgeDisplayLabel(edge, false);
      if (labelText && (Math.abs(x1 - x2) > 14 || Math.abs(y1 - y2) > 24)) {
        edgeLabelGroup.append('text')
          .attr('x', (x1 + x2) / 2)
          .attr('y', midY - 2)
          .attr('text-anchor', 'middle')
          .attr('dominant-baseline', 'central')
          .attr('fill', style.color)
          .attr('fill-opacity', dimmed ? 0.08 : 0.6)
          .attr('font-size', '8px')
          .attr('font-family', FONT.mono)
          .attr('letter-spacing', '0.08em')
          .style('paint-order', 'stroke')
          .style('stroke', C.void)
          .style('stroke-width', '3px')
          .style('stroke-linejoin', 'round')
          .style('pointer-events', 'none')
          .text(labelText);
      }
    }

    // --- Draw nodes ---
    const nodeGroup = g.append('g');

    for (const dagNode of dag.nodes()) {
      const d: StructureNode = dagNode.data;
      const x = dagNode.x! + pad;
      const y = dagNode.y! + pad;
      const w = nodeWidth(d);
      const h = nodeBoxHeight(d);
      const dimmed = highlightedIds && !highlightedIds.has(d.id);

      const nodeG = nodeGroup.append('g')
        .attr('transform', `translate(${x},${y})`)
        .attr('opacity', dimmed ? 0.12 : 1)
        .style('cursor', 'pointer')
        .style('transition', 'opacity 0.2s ease');

      drawNodeShape(nodeG as any, d, w, h, jurisdictionColorMap);

      // Name label (multi-line)
      const labelLines = nodeLabelLines(d.name);
      const lineHeight = 12;
      const textEl = nodeG.append('text')
        .attr('text-anchor', 'middle')
        .attr('dominant-baseline', 'central')
        .attr('fill', C.moonlight)
        .attr('font-size', '11px')
        .attr('font-family', FONT.ui)
        .attr('font-weight', d.emphasis === 'primary' ? '600' : '500')
        .attr('letter-spacing', '0.01em')
        .style('pointer-events', 'none');

      labelLines.forEach((line, i) => {
        textEl.append('tspan')
          .attr('x', 0)
          .attr('dy', i === 0 ? -((labelLines.length - 1) * lineHeight) / 2 : lineHeight)
          .text(line);
      });

      // Sub-label: role + entity type + jurisdiction
      const subLabel = nodeSubLabel(d);
      if (subLabel) {
        nodeG.append('text')
          .attr('y', h / 2 + 14)
          .attr('text-anchor', 'middle')
          .attr('fill', C.mithril)
          .attr('font-size', '8.5px')
          .attr('font-family', FONT.mono)
          .attr('letter-spacing', '0.08em')
          .style('pointer-events', 'none')
          .text(subLabel);
      }

      // Collapse indicator
      const numChildren = childCounts.get(d.id) || 0;
      if (numChildren > 0) {
        const isCollapsed = collapsedSet.has(d.id);
        const indicator = nodeG.append('g')
          .attr('transform', `translate(${w / 2 - 2}, ${-h / 2 + 2})`)
          .style('cursor', 'pointer');

        indicator.append('circle')
          .attr('r', 7)
          .attr('fill', C.slate)
          .attr('stroke', C.ash)
          .attr('stroke-width', 1);

        indicator.append('text')
          .attr('text-anchor', 'middle')
          .attr('dominant-baseline', 'central')
          .attr('fill', C.moonlight)
          .attr('font-size', '10px')
          .attr('font-family', FONT.ui)
          .attr('font-weight', '700')
          .style('pointer-events', 'none')
          .text(isCollapsed ? '+' : '\u2212');

        indicator.on('click', (event: MouseEvent) => {
          event.stopPropagation();
          setCollapsedSet(prev => {
            const next = new Set(prev);
            if (next.has(d.id)) next.delete(d.id);
            else next.add(d.id);
            return next;
          });
        });
      }

      // Hover
      nodeG.on('mouseover', (event: MouseEvent) => {
        const lines = [`<strong>${d.name}</strong>`];
        if (d.nodeType === 'entity') {
          if (d.role) lines.push(`Role: ${d.role}`);
          if (d.entityType) lines.push(`Type: ${d.entityType}`);
          if (d.jurisdiction) lines.push(`Jurisdiction: ${d.jurisdiction}`);
          if (d.status && d.status !== 'active') lines.push(`Status: ${d.status}`);
        }
        const connected = visibleEdges.filter(e => e.source === d.id || e.target === d.id);
        for (const e of connected.slice(0, 4)) {
          const dir = e.source === d.id ? '\u2192' : '\u2190';
          const other = e.source === d.id ? e.target : e.source;
          const otherName = visibleNodes.find(n => n.id === other)?.name || other;
          lines.push(`<span style="color:${C.mithril};font-size:11px">${dir} ${otherName} <em>(${e.relationType})</em></span>`);
        }
        if (connected.length > 4) {
          lines.push(`<span style="color:${C.mithril};font-size:11px">+${connected.length - 4} more</span>`);
        }
        tooltip.style('visibility', 'visible').html(lines.join('<br/>'));
      })
        .on('mousemove', (event: MouseEvent) => {
          tooltip.style('top', (event.pageY - 10) + 'px').style('left', (event.pageX + 14) + 'px');
        })
        .on('mouseout', () => tooltip.style('visibility', 'hidden'));

      // Click to highlight chain
      nodeG.on('click', () => {
        if (highlightedIds?.has(d.id) && highlightedIds.size > 1) {
          setHighlightedIds(null);
          return;
        }
        const anc = ancestors.get(d.id) || new Set();
        const desc = descendants.get(d.id) || new Set();
        setHighlightedIds(new Set([d.id, ...anc, ...desc]));
      });
    }

    // --- Annotation markers (optional) ---
    if (data.annotations && data.annotations.length > 0) {
      const annotationGroup = g.append('g').style('pointer-events', 'none');
      data.annotations.forEach((annotation, idx) => {
        const target = posMap.get(annotation.targetId);
        const targetNode = nodeById.get(annotation.targetId);
        if (!target || !targetNode) return;

        const nodeW = nodeWidth(targetNode);
        const nodeH = nodeBoxHeight(targetNode);
        const baseX = target.x + pad;
        const baseY = target.y + pad;
        const align = annotation.align || 'right';
        const dx = annotation.dx ?? 0;
        const dy = annotation.dy ?? 0;

        const cx = align === 'left'
          ? baseX - nodeW / 2 + 10 + dx
          : align === 'right'
            ? baseX + nodeW / 2 - 10 + dx
            : baseX + dx;
        const cy = align === 'top'
          ? baseY - nodeH / 2 + 10 + dy
          : align === 'bottom'
            ? baseY + nodeH / 2 - 10 + dy
            : baseY - nodeH / 2 + 10 + dy;

        annotationGroup.append('circle')
          .attr('cx', cx)
          .attr('cy', cy)
          .attr('r', 8)
          .attr('fill', C.slate)
          .attr('stroke', C.ash)
          .attr('stroke-width', 1);

        annotationGroup.append('text')
          .attr('x', cx)
          .attr('y', cy + 0.5)
          .attr('text-anchor', 'middle')
          .attr('dominant-baseline', 'central')
          .attr('fill', C.moonlight)
          .attr('font-size', '9px')
          .attr('font-family', FONT.mono)
          .attr('font-weight', '600')
          .text(idx + 1);
      });
    }

    return () => { tooltip.remove(); };
  }, [visibleNodes, visibleEdges, edgesForRender, nodeById, jurisdictionColorMap, containerWidth, height, collapsedSet, highlightedIds, childCounts, ancestors, descendants]);

  return (
    <div ref={containerRef} className="surface p-5">
      <div className="mb-4">
        <h3 className="text-lg text-moon" style={{ fontFamily: 'var(--font-ui)', fontWeight: 600 }}>
          {data.title}
        </h3>
        {data.subtitle && (
          <p className="text-sm text-mithril mt-1" style={{ fontFamily: 'var(--font-body)' }}>
            {data.subtitle}
          </p>
        )}
      </div>
      <svg ref={svgRef} className="w-full graph-canvas" style={{ minHeight: '300px', maxHeight: `${height}px`, borderRadius: '4px' }} />

      {data.annotations && data.annotations.length > 0 && (
        <div className="mt-4 grid gap-2 text-xs text-mithril" style={{ fontFamily: 'var(--font-body)' }}>
          <div className="text-moon mb-1" style={{ fontSize: '0.65rem', letterSpacing: '0.2em', textTransform: 'uppercase', fontFamily: 'var(--font-ui)', fontWeight: 500 }}>
            Key Notes
          </div>
          {data.annotations.map((annotation, index) => (
            <div key={annotation.id} className="flex items-start gap-2">
              <span
                className="inline-flex items-center justify-center"
                style={{
                  width: '18px',
                  height: '18px',
                  borderRadius: '999px',
                  border: `1px solid ${C.ash}`,
                  color: C.moonlight,
                  fontFamily: 'var(--font-mono)',
                  fontSize: '0.6rem',
                  flex: '0 0 auto',
                  lineHeight: 1,
                }}
              >
                {index + 1}
              </span>
              <span>{annotation.text}</span>
            </div>
          ))}
        </div>
      )}

      {/* Legend */}
      <div className="mt-4 pt-3 flex flex-wrap gap-6" style={{ borderTop: `1px solid ${C.ash}`, fontFamily: 'var(--font-ui)' }}>
        <div>
          <div className="text-moon mb-1.5" style={{ fontSize: '0.65rem', letterSpacing: '0.2em', textTransform: 'uppercase', fontWeight: 500 }}>
            Entity Shapes
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-mithril">
            {shapeLegendEntries.map(s => (
              <div key={s.kind} className="flex items-center gap-1.5">
                <svg width="18" height="14" viewBox="0 0 18 14">
                  {s.kind === 'person' ? (
                    <ellipse cx="9" cy="7" rx="8" ry="6" fill={C.stone} fillOpacity={0.95} stroke={C.mithril} strokeWidth={0.8} />
                  ) : s.kind === 'trust' ? (
                    <polygon points="9,1 17,5 17,13 1,13 1,5" fill={C.stone} fillOpacity={0.95} stroke={C.mithril} strokeWidth={0.8} />
                  ) : s.kind === 'foundation' ? (
                    <rect x="1" y="1" width="16" height="12" rx="5" fill={C.stone} fillOpacity={0.95} stroke={C.mithril} strokeWidth={0.8} />
                  ) : s.kind === 'fund' ? (
                    <polygon points="4,1 17,1 14,13 1,13" fill={C.stone} fillOpacity={0.95} stroke={C.mithril} strokeWidth={0.8} />
                  ) : s.kind === 'holding' ? (
                    <polygon points="4,1 14,1 17,7 14,13 4,13 1,7" fill={C.stone} fillOpacity={0.95} stroke={C.mithril} strokeWidth={0.8} />
                  ) : (
                    <rect x="1" y="1" width="16" height="12" rx="2" fill={C.stone} fillOpacity={0.95} stroke={C.mithril} strokeWidth={0.8} />
                  )}
                </svg>
                <span>{s.label}</span>
              </div>
            ))}
          </div>
        </div>
        <div>
          <div className="text-moon mb-1.5" style={{ fontSize: '0.65rem', letterSpacing: '0.2em', textTransform: 'uppercase', fontWeight: 500 }}>
            Jurisdiction (Stroke)
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-mithril">
            {jurisdictionEntries.map(j => (
              <div key={j.label} className="flex items-center gap-1.5">
                <span
                  className="inline-block w-3 h-3 rounded-sm"
                  style={{ border: `1.5px solid ${j.color}`, background: 'transparent' }}
                />
                <span>{j.label}</span>
              </div>
            ))}
          </div>
        </div>
        <div>
          <div className="text-moon mb-1.5" style={{ fontSize: '0.65rem', letterSpacing: '0.2em', textTransform: 'uppercase', fontWeight: 500 }}>
            Relationships
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-mithril">
            {relationshipLegendEntries.map(entry => {
              const color = RELATIONSHIP_COLORS[entry.category] || RELATIONSHIP_COLORS.other;
              const dash = RELATIONSHIP_DASH[entry.category] || '';
              return (
                <div key={entry.label} className="flex items-center gap-1.5">
                  <svg width="26" height="8" viewBox="0 0 26 8">
                    <line x1="0" y1="4" x2="20" y2="4" stroke={color} strokeWidth={2} strokeDasharray={dash} />
                    <polygon points="20,1 26,4 20,7" fill={color} />
                  </svg>
                  <span>{entry.label}</span>
                </div>
              );
            })}
          </div>
        </div>
        {evidenceLegendEntries.length > 0 && (
          <div>
            <div className="text-moon mb-1.5" style={{ fontSize: '0.65rem', letterSpacing: '0.2em', textTransform: 'uppercase', fontWeight: 500 }}>
              Evidence
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-mithril">
              {evidenceLegendEntries.map(entry => (
                <div key={entry.label} className="flex items-center gap-1.5">
                  <svg width="26" height="8" viewBox="0 0 26 8">
                    <line x1="0" y1="4" x2="20" y2="4" stroke={C.mithril} strokeWidth={2} strokeDasharray={entry.dash} />
                  </svg>
                  <span>{entry.label}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {highlightedIds && (
        <button
          onClick={() => setHighlightedIds(null)}
          className="mt-2 text-icy transition-colors"
          style={{ fontSize: '0.7rem', letterSpacing: '0.12em', textTransform: 'uppercase', fontFamily: 'var(--font-ui)' }}
        >
          Clear highlight
        </button>
      )}
    </div>
  );
}
