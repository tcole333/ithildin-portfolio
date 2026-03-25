import { useEffect, useMemo, useRef, useState } from 'react';
import * as d3 from 'd3';

interface NetworkNode {
  id: string;
  name: string;
  slug?: string;
  type: 'person' | 'entity';
  connections: number;
  finding_count?: number;
  entity_type?: string;
  jurisdiction?: string;
  // D3 simulation fields
  x?: number;
  y?: number;
  fx?: number | null;
  fy?: number | null;
  color?: string;
  baseColor?: string;
  val?: number;
  distance?: number | null;
}

interface NetworkEdge {
  source: string | NetworkNode;
  target: string | NetworkNode;
  relationship_type: string;
  description: string;
  strength: string;
  verified: boolean;
  strengthWeight?: number;
  strengthOpacity?: number;
}

interface NetworkData {
  nodes: NetworkNode[];
  edges: NetworkEdge[];
  stats: {
    total_nodes: number;
    person_nodes: number;
    entity_nodes: number;
    total_edges: number;
  };
}

interface Props {
  data: NetworkData;
  dossierSlugs?: string[];
  primarySubjectId?: string;
}

const COLORS = {
  person: '#c7d0d9',
  entity: '#7ea7c1',
  highlight: '#8fd3e8',
  edge: '#2a313b',
  text: '#c7d0d9',
  muted: '#8c97a3',
  panelBg: '#12151b',
  panelBorder: '#2a313b',
};

const STRENGTH_WIDTH: Record<string, number> = {
  strong: 2.6,
  medium: 1.8,
  weak: 1.2,
  circumstantial: 1,
};

const STRENGTH_OPACITY: Record<string, number> = {
  strong: 0.6,
  medium: 0.45,
  weak: 0.35,
  circumstantial: 0.3,
};

// Default primary subject ID — overridden by primarySubjectId prop
const DEFAULT_PRIMARY_SUBJECT = 'Jeffrey Epstein';

function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, '')
    .replace(/[\s-]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

function formatRelationship(value?: string): string {
  if (!value) return 'Unknown';
  return value.replace(/_/g, ' ');
}

function formatStrength(value?: string): string {
  if (!value) return 'unknown';
  return value.replace(/_/g, ' ');
}

function formatEntityType(value?: string): string {
  if (!value) return 'unknown';
  return value.replace(/_/g, ' ');
}

export default function NetworkGraph({ data, dossierSlugs = [], primarySubjectId }: Props) {
  const PRIMARY_ID = primarySubjectId || DEFAULT_PRIMARY_SUBJECT;
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const tooltipRef = useRef<d3.Selection<HTMLDivElement, unknown, HTMLElement, any> | null>(null);
  const [dimensions, setDimensions] = useState({ width: 0, height: 0 });

  const nodeById = useMemo(() => {
    const map = new Map<string, NetworkNode>();
    data.nodes.forEach(node => map.set(node.id, node));
    return map;
  }, [data]);

  const dossierSlugSet = useMemo(() => new Set(dossierSlugs), [dossierSlugs]);

  const entityTypeStats = useMemo(() => {
    const counts = new Map<string, number>();
    data.nodes.forEach(node => {
      if (node.type !== 'entity') return;
      const type = node.entity_type || 'unknown';
      counts.set(type, (counts.get(type) || 0) + 1);
    });
    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);
  }, [data]);

  const relationshipStats = useMemo(() => {
    const counts = new Map<string, number>();
    data.edges.forEach(edge => {
      const type = edge.relationship_type || 'unknown';
      counts.set(type, (counts.get(type) || 0) + 1);
    });
    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);
  }, [data]);

  const strengthStats = useMemo(() => {
    const counts = new Map<string, number>();
    data.edges.forEach(edge => {
      const strength = edge.strength || 'unknown';
      counts.set(strength, (counts.get(strength) || 0) + 1);
    });
    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);
  }, [data]);

  const [query, setQuery] = useState('');
  const [selectedNodes, setSelectedNodes] = useState<string[]>([]);
  const [focusMode, setFocusMode] = useState(false);
  const [depth, setDepth] = useState(2);
  const [includePersons, setIncludePersons] = useState(true);
  const [excludePrimary, setExcludePrimary] = useState(false);
  const [selectedEntityTypes, setSelectedEntityTypes] = useState(() => entityTypeStats.map(([type]) => type));
  const [selectedRelTypes, setSelectedRelTypes] = useState(() => relationshipStats.map(([type]) => type));
  const [selectedStrengths, setSelectedStrengths] = useState(() => strengthStats.map(([type]) => type));
  const [verifiedOnly, setVerifiedOnly] = useState(false);
  const [showEdgeLabels, setShowEdgeLabels] = useState(false);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const update = () => {
      const rect = container.getBoundingClientRect();
      const width = Math.floor(rect.width);
      const height = Math.floor(rect.height);
      if (width && height) {
        setDimensions({ width, height });
      }
    };

    update();

    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', update);
      return () => window.removeEventListener('resize', update);
    }

    const observer = new ResizeObserver(update);
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  const searchResults = useMemo(() => {
    const term = query.trim().toLowerCase();
    if (!term) return [];
    return data.nodes
      .filter(node => !(excludePrimary && node.id === PRIMARY_ID))
      .filter(node => (node.name || node.id).toLowerCase().includes(term))
      .sort((a, b) => (b.connections || 0) - (a.connections || 0))
      .slice(0, 8);
  }, [data, query, excludePrimary]);

  const graphState = useMemo(() => {
    const selectedSet = new Set(selectedNodes);
    const allowedEntityTypes = new Set(selectedEntityTypes);
    const allowedRelTypes = new Set(selectedRelTypes);
    const allowedStrengths = new Set(selectedStrengths);

    const filteredNodes = data.nodes.filter(node => {
      if (excludePrimary && node.id === PRIMARY_ID) return false;
      if (selectedSet.has(node.id)) return true;
      if (node.type === 'person') return includePersons;
      return allowedEntityTypes.has(node.entity_type || 'unknown');
    });

    const nodeIds = new Set(filteredNodes.map(node => node.id));

    const filteredEdges = data.edges.filter(edge => {
      const sid = typeof edge.source === 'string' ? edge.source : edge.source.id;
      const tid = typeof edge.target === 'string' ? edge.target : edge.target.id;
      if (!nodeIds.has(sid) || !nodeIds.has(tid)) return false;
      if (!allowedRelTypes.has(edge.relationship_type || 'unknown')) return false;
      if (!allowedStrengths.has(edge.strength || 'unknown')) return false;
      if (verifiedOnly && !edge.verified) return false;
      return true;
    });

    const adjacency = new Map<string, Set<string>>();
    filteredEdges.forEach(edge => {
      const sid = typeof edge.source === 'string' ? edge.source : edge.source.id;
      const tid = typeof edge.target === 'string' ? edge.target : edge.target.id;
      if (!adjacency.has(sid)) adjacency.set(sid, new Set());
      if (!adjacency.has(tid)) adjacency.set(tid, new Set());
      adjacency.get(sid)!.add(tid);
      adjacency.get(tid)!.add(sid);
    });

    const distances: Record<string, number> = {};
    if (selectedSet.size > 0) {
      const queue: string[] = [];
      selectedSet.forEach(id => {
        distances[id] = 0;
        queue.push(id);
      });

      while (queue.length > 0) {
        const current = queue.shift()!;
        const dist = distances[current];
        const neighbors = adjacency.get(current);
        if (!neighbors) continue;
        for (const neighbor of neighbors) {
          if (distances[neighbor] === undefined) {
            distances[neighbor] = dist + 1;
            queue.push(neighbor);
          }
        }
      }
    }

    if (focusMode && selectedSet.size === 0) {
      return {
        nodes: [],
        edges: [],
        distances,
        hasSelection: false,
        message: 'Select one or more entities to focus the network.',
      };
    }

    let focusedNodes = filteredNodes;
    let focusedEdges = filteredEdges;

    if (focusMode && selectedSet.size > 0) {
      const keepIds = new Set<string>();
      Object.entries(distances).forEach(([id, dist]) => {
        if (dist <= depth) keepIds.add(id);
      });

      focusedNodes = filteredNodes.filter(node => keepIds.has(node.id));
      const focusedNodeIds = new Set(focusedNodes.map(node => node.id));
      focusedEdges = filteredEdges.filter(edge => {
        const sid = typeof edge.source === 'string' ? edge.source : edge.source.id;
        const tid = typeof edge.target === 'string' ? edge.target : edge.target.id;
        return focusedNodeIds.has(sid) && focusedNodeIds.has(tid);
      });
    }

    const nodes = focusedNodes.map(node => {
      const baseColor = node.type === 'person' ? COLORS.person : COLORS.entity;
      return {
        ...node,
        val: Math.max(1, node.connections || 1),
        baseColor,
        color: baseColor,
        distance: distances[node.id] ?? null,
      };
    });

    const edges = focusedEdges.map(edge => {
      const strengthKey = (edge.strength || 'medium').toLowerCase();
      return {
        ...edge,
        strengthWeight: STRENGTH_WIDTH[strengthKey] || 1.6,
        strengthOpacity: STRENGTH_OPACITY[strengthKey] || 0.4,
      };
    });

    return {
      nodes,
      edges,
      distances,
      hasSelection: selectedSet.size > 0,
      message: '',
    };
  }, [
    data,
    selectedNodes,
    includePersons,
    selectedEntityTypes,
    selectedRelTypes,
    selectedStrengths,
    verifiedOnly,
    focusMode,
    depth,
    excludePrimary,
  ]);

  const labelIds = useMemo(() => {
    const ids = new Set<string>();
    const sorted = [...graphState.nodes].sort((a, b) => (b.connections || 0) - (a.connections || 0));
    const limit = graphState.nodes.length > 240 ? 40 : graphState.nodes.length > 140 ? 60 : 120;
    sorted.slice(0, limit).forEach(node => ids.add(node.id));
    selectedNodes.forEach(id => ids.add(id));
    return ids;
  }, [graphState.nodes, selectedNodes]);

  useEffect(() => {
    if (!svgRef.current) return;
    if (graphState.nodes.length === 0) {
      d3.select(svgRef.current).selectAll('*').remove();
      return;
    }

    const width = dimensions.width;
    const height = dimensions.height;
    if (!width || !height) return;

    d3.select(svgRef.current).selectAll('*').remove();
    const svg = d3.select(svgRef.current)
      .attr('width', width)
      .attr('height', height)
      .attr('viewBox', `0 0 ${width} ${height}`);

    const zoom = d3.zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.2, 6])
      .on('zoom', (event) => g.attr('transform', event.transform));

    const g = svg.append('g');

    svg.call(zoom as any);

    const maxConn = Math.max(...graphState.nodes.map(n => n.val || 1));
    const radiusScale = d3.scaleSqrt()
      .domain([1, maxConn])
      .range([4, 36])
      .clamp(true);

    const nodes = graphState.nodes.map(node => ({ ...node }));
    const edges = graphState.edges.map(edge => ({ ...edge }));

    const simulation = d3.forceSimulation(nodes as any)
      .alphaDecay(0.05)
      .alphaMin(0.02)
      .force('link', d3.forceLink(edges as any).id((d: any) => d.id).distance(60))
      .force('charge', d3.forceManyBody().strength(-220))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collision', d3.forceCollide().radius((d: any) => radiusScale(d.val || 1) + 6))
      .force('radial', d3.forceRadial(
        (d: any) => (50 - Math.min(d.val || 1, 50)) * 24 + 200,
        width / 2,
        height / 2,
      ).strength(0.1));

    const link = g.append('g')
      .selectAll('line')
      .data(edges)
      .join('line')
      .attr('stroke', COLORS.edge)
      .attr('stroke-width', (d: any) => d.strengthWeight || 1.6)
      .attr('stroke-opacity', (d: any) => d.strengthOpacity || 0.4)
      .attr('stroke-dasharray', (d: any) => d.verified ? '0' : '4 4');

    const labelEdges = showEdgeLabels && edges.length <= 120;
    const edgeLabels = labelEdges
      ? g.append('g')
        .selectAll('text')
        .data(edges)
        .join('text')
        .attr('fill', COLORS.text)
        .attr('font-size', '9px')
        .style('paint-order', 'stroke')
        .style('stroke', '#0b0d10')
        .style('stroke-width', '3px')
        .style('stroke-linejoin', 'round')
        .style('pointer-events', 'none')
        .text((d: any) => formatRelationship(d.relationship_type))
      : null;

    const node = g.append('g')
      .selectAll('g')
      .data(nodes)
      .join('g')
      .call(d3.drag<any, any>()
        .on('start', (event, d) => { d.fx = d.x; d.fy = d.y; })
        .on('drag', (event, d) => {
          d.fx = event.x; d.fy = event.y;
          if (!event.active) simulation.alphaTarget(0.3).restart();
        })
        .on('end', (event, d) => {
          if (!event.active) simulation.alphaTarget(0);
          d.fx = null; d.fy = null;
        }) as any);

    node.append('circle')
      .attr('r', (d: any) => radiusScale(d.val || 1))
      .attr('fill', (d: any) => d.color)
      .attr('stroke', COLORS.text)
      .attr('stroke-width', 0.5)
      .style('cursor', 'pointer')
      .on('click', (event: any, d: any) => {
        const multi = event.metaKey || event.ctrlKey || event.shiftKey;
        setSelectedNodes(prev => {
          const next = new Set(prev);
          if (multi) {
            if (next.has(d.id)) {
              next.delete(d.id);
            } else {
              next.add(d.id);
            }
            return Array.from(next);
          }
          if (prev.length === 1 && prev[0] === d.id) return [];
          return [d.id];
        });
      });

    const labelNodes = nodes.filter(nodeItem => labelIds.has(nodeItem.id));

    const nodeLabels = g.append('g')
      .selectAll('text')
      .data(labelNodes)
      .join('text')
      .text((d: any) => d.name)
      .attr('text-anchor', 'middle')
      .attr('fill', COLORS.text)
      .attr('font-size', '10px')
      .style('paint-order', 'stroke')
      .style('stroke', '#0b0d10')
      .style('stroke-width', '3px')
      .style('stroke-linejoin', 'round')
      .style('pointer-events', 'none');

    if (!tooltipRef.current) {
      tooltipRef.current = d3.select('body')
        .append('div')
        .style('position', 'absolute')
        .style('visibility', 'hidden')
        .style('background', COLORS.panelBg)
        .style('color', COLORS.text)
        .style('border', `1px solid ${COLORS.panelBorder}`)
        .style('padding', '8px 12px')
        .style('border-radius', '6px')
        .style('font-size', '12px')
        .style('pointer-events', 'none')
        .style('z-index', '1000');
    }
    const tooltip = tooltipRef.current;
    tooltip.style('visibility', 'hidden');

    node.on('mouseover', (_event: any, d: any) => {
      tooltip.style('visibility', 'visible')
        .html(`<strong>${d.name}</strong><br/>${d.connections} connections${d.finding_count ? `<br/>${d.finding_count} findings` : ''}${d.type === 'entity' ? `<br/>${formatEntityType(d.entity_type)} (${d.jurisdiction || '?'})` : ''}`);
    })
      .on('mousemove', (event: any) => {
        tooltip.style('top', (event.pageY - 10) + 'px').style('left', (event.pageX + 10) + 'px');
      })
      .on('mouseout', () => tooltip.style('visibility', 'hidden'));

    link.on('mouseover', function (_event: any, d: any) {
      d3.select(this).attr('stroke-opacity', 0.8);
      tooltip.style('visibility', 'visible')
        .html(`<strong>${formatRelationship(d.relationship_type)}</strong><br/>Strength: ${formatStrength(d.strength)}${d.verified ? '' : '<br/>Unverified'}${d.description ? `<br/>${d.description}` : ''}`);
    })
      .on('mousemove', (event: any) => {
        tooltip.style('top', (event.pageY - 10) + 'px').style('left', (event.pageX + 10) + 'px');
      })
      .on('mouseout', function () {
        d3.select(this).attr('stroke-opacity', (d: any) => d.strengthOpacity || 0.4);
        tooltip.style('visibility', 'hidden');
      });

    simulation.on('tick', () => {
      link
        .attr('x1', (d: any) => d.source.x)
        .attr('y1', (d: any) => d.source.y)
        .attr('x2', (d: any) => d.target.x)
        .attr('y2', (d: any) => d.target.y);

      if (edgeLabels) {
        edgeLabels
          .attr('x', (d: any) => (d.source.x + d.target.x) / 2)
          .attr('y', (d: any) => (d.source.y + d.target.y) / 2);
      }

      node.attr('transform', (d: any) => `translate(${d.x},${d.y})`);
      nodeLabels
        .attr('x', (d: any) => d.x)
        .attr('y', (d: any) => d.y + radiusScale(d.val || 1) + 8);
    });

    return () => {
      simulation.stop();
      tooltip.style('visibility', 'hidden');
    };
  }, [graphState, dimensions, labelIds, showEdgeLabels]);

  useEffect(() => {
    if (!svgRef.current) return;

    const selectedSet = new Set(selectedNodes);
    const distances = graphState.distances;
    const svg = d3.select(svgRef.current);

    svg.selectAll<SVGCircleElement, any>('circle')
      .attr('fill', (d: any) => selectedSet.has(d.id) ? COLORS.highlight : d.baseColor)
      .attr('fill-opacity', (d: any) => {
        if (selectedSet.size === 0) return 0.9;
        if (selectedSet.has(d.id)) return 1;
        const dist = distances[d.id];
        if (dist === 1) return 0.85;
        if (dist === 2) return 0.65;
        return 0.35;
      });

    svg.selectAll<SVGLineElement, any>('line')
      .attr('stroke', (d: any) => {
        const sid = typeof d.source === 'string' ? d.source : d.source.id;
        const tid = typeof d.target === 'string' ? d.target : d.target.id;
        return selectedSet.size > 0 && (selectedSet.has(sid) || selectedSet.has(tid))
          ? COLORS.highlight
          : COLORS.edge;
      })
      .attr('stroke-opacity', (d: any) => {
        if (selectedSet.size === 0) return d.strengthOpacity || 0.4;
        const sid = typeof d.source === 'string' ? d.source : d.source.id;
        const tid = typeof d.target === 'string' ? d.target : d.target.id;
        return selectedSet.has(sid) || selectedSet.has(tid) ? 0.85 : 0.2;
      });
  }, [selectedNodes, graphState.distances]);

  useEffect(() => {
    if (!excludePrimary) return;
    setSelectedNodes(prev => prev.filter(id => id !== PRIMARY_ID));
  }, [excludePrimary]);

  const selectedDetails = selectedNodes
    .map(id => nodeById.get(id))
    .filter(Boolean) as NetworkNode[];

  const resetFilters = () => {
    setIncludePersons(true);
    setExcludePrimary(false);
    setSelectedEntityTypes(entityTypeStats.map(([type]) => type));
    setSelectedRelTypes(relationshipStats.map(([type]) => type));
    setSelectedStrengths(strengthStats.map(([type]) => type));
    setVerifiedOnly(false);
  };

  const toggleSelection = (id: string) => {
    setSelectedNodes(prev => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return Array.from(next);
    });
  };

  return (
    <div ref={containerRef} className="relative w-full h-full">
      <div className="absolute top-4 left-4 z-10 w-80 max-h-[70vh] overflow-y-auto">
        <div className="surface p-4 space-y-4">
          <div>
            <div className="section-label">Search</div>
            <input
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="Search entities"
              className="mt-2 w-full rounded border border-[color:var(--color-ash)] bg-[rgba(11,13,16,0.6)] px-3 py-2 text-sm text-moon placeholder:text-[color:var(--color-mithril)]"
            />
            {searchResults.length > 0 && (
              <div className="mt-2 space-y-1">
                {searchResults.map(node => (
                  <button
                    key={node.id}
                    onClick={() => toggleSelection(node.id)}
                    className={`w-full text-left rounded border border-[color:var(--color-ash)] px-3 py-2 text-xs transition ${selectedNodes.includes(node.id) ? 'bg-[rgba(18,21,27,0.85)]' : 'bg-[rgba(18,21,27,0.55)]'}`}
                  >
                    <div className="text-sm text-moon">{node.name}</div>
                    <div className="text-xs text-mithril">
                      {node.type}{node.type === 'entity' && node.entity_type ? ` / ${formatEntityType(node.entity_type)}` : ''} / {node.connections} links
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>

          <div>
            <div className="flex items-center justify-between">
              <div className="section-label">Selection</div>
              {selectedNodes.length > 0 && (
                <button onClick={() => setSelectedNodes([])} className="text-xs text-mithril hover:text-moon">Clear</button>
              )}
            </div>
            {selectedDetails.length === 0 ? (
              <p className="text-xs text-mithril mt-2">No entities selected.</p>
            ) : (
              <div className="flex flex-wrap gap-2 mt-2">
                {selectedDetails.map(node => (
                  <span key={node.id} className="chip" style={{ letterSpacing: '0.12em' }}>
                    {node.name}
                    <button onClick={() => toggleSelection(node.id)} className="text-[10px]">x</button>
                  </span>
                ))}
              </div>
            )}
            {selectedDetails.length === 1 && (
              <div className="mt-3 text-xs text-mithril">
                <div className="text-moon">{selectedDetails[0].connections} connections</div>
                {selectedDetails[0].finding_count ? (
                  <div>{selectedDetails[0].finding_count} findings</div>
                ) : null}
                {selectedDetails[0].type === 'entity' && (
                  <div>{formatEntityType(selectedDetails[0].entity_type)} / {selectedDetails[0].jurisdiction || 'Unknown jurisdiction'}</div>
                )}
                {(() => {
                  const slug = selectedDetails[0].slug || slugify(selectedDetails[0].name || selectedDetails[0].id);
                  if (!dossierSlugSet.has(slug)) return <div className="mt-2">No dossier yet.</div>;
                  return (
                    <a
                      href={`/dossiers/${slug}`}
                      className="mt-2 inline-flex text-icy hover:text-ember"
                    >
                      View dossier &rarr;
                    </a>
                  );
                })()}
              </div>
            )}
          </div>

          <div className="surface-muted p-3 space-y-3">
            <div className="flex items-center justify-between">
              <div className="section-label">Focus</div>
              <label className="text-xs text-mithril flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={focusMode}
                  onChange={e => setFocusMode(e.target.checked)}
                />
                Focus selection
              </label>
            </div>
            <div>
              <div className="text-xs text-mithril">Depth: {depth}</div>
              <input
                type="range"
                min={1}
                max={4}
                value={depth}
                onChange={e => setDepth(Number(e.target.value))}
                className="w-full"
                disabled={!focusMode}
              />
            </div>
            {graphState.message && (
              <div className="text-xs text-ember">{graphState.message}</div>
            )}
          </div>

          <details className="surface-muted p-3">
            <summary className="text-xs text-mithril uppercase tracking-wider">Filters</summary>
            <div className="mt-3 space-y-4">
              <div>
                <div className="section-label">Entities</div>
                <label className="mt-2 flex items-center gap-2 text-xs text-mithril">
                  <input
                    type="checkbox"
                    checked={includePersons}
                    onChange={e => setIncludePersons(e.target.checked)}
                  />
                  People ({data.stats.person_nodes})
                </label>
                <label className="mt-2 flex items-center gap-2 text-xs text-mithril">
                  <input
                    type="checkbox"
                    checked={excludePrimary}
                    onChange={e => setExcludePrimary(e.target.checked)}
                  />
                  Exclude {PRIMARY_ID}
                </label>
                <div className="mt-2 grid grid-cols-2 gap-2">
                  {entityTypeStats.map(([type, count]) => (
                    <label key={type} className="flex items-center gap-2 text-xs text-mithril">
                      <input
                        type="checkbox"
                        checked={selectedEntityTypes.includes(type)}
                        onChange={e => {
                          setSelectedEntityTypes(prev =>
                            e.target.checked
                              ? [...prev, type]
                              : prev.filter(value => value !== type)
                          );
                        }}
                      />
                      {formatEntityType(type)} ({count})
                    </label>
                  ))}
                </div>
              </div>

              <div>
                <div className="section-label">Relationships</div>
                <div className="mt-2 grid grid-cols-2 gap-2">
                  {relationshipStats.map(([type, count]) => (
                    <label key={type} className="flex items-center gap-2 text-xs text-mithril">
                      <input
                        type="checkbox"
                        checked={selectedRelTypes.includes(type)}
                        onChange={e => {
                          setSelectedRelTypes(prev =>
                            e.target.checked
                              ? [...prev, type]
                              : prev.filter(value => value !== type)
                          );
                        }}
                      />
                      {formatRelationship(type)} ({count})
                    </label>
                  ))}
                </div>
              </div>

              <div>
                <div className="section-label">Strength</div>
                <div className="mt-2 grid grid-cols-2 gap-2">
                  {strengthStats.map(([type, count]) => (
                    <label key={type} className="flex items-center gap-2 text-xs text-mithril">
                      <input
                        type="checkbox"
                        checked={selectedStrengths.includes(type)}
                        onChange={e => {
                          setSelectedStrengths(prev =>
                            e.target.checked
                              ? [...prev, type]
                              : prev.filter(value => value !== type)
                          );
                        }}
                      />
                      {formatStrength(type)} ({count})
                    </label>
                  ))}
                </div>
                <label className="mt-2 flex items-center gap-2 text-xs text-mithril">
                  <input
                    type="checkbox"
                    checked={verifiedOnly}
                    onChange={e => setVerifiedOnly(e.target.checked)}
                  />
                  Verified only
                </label>
                <label className="mt-2 flex items-center gap-2 text-xs text-mithril">
                  <input
                    type="checkbox"
                    checked={showEdgeLabels}
                    onChange={e => setShowEdgeLabels(e.target.checked)}
                  />
                  Edge labels
                </label>
              </div>

              <button
                onClick={resetFilters}
                className="text-xs text-mithril hover:text-moon"
              >
                Reset filters
              </button>
            </div>
          </details>

          <div className="text-xs text-mithril">
            {graphState.nodes.length} nodes / {graphState.edges.length} edges shown
          </div>
          <div className="text-[11px] text-mithril">
            Edge width = strength - dashed = unverified
          </div>
        </div>
      </div>

      <svg ref={svgRef} className="w-full h-full graph-canvas" />

      <div className="absolute bottom-0 left-0 right-0 bg-[rgba(18,21,27,0.75)] backdrop-blur-sm px-4 py-2 text-xs text-mithril text-center">
        Drag to pan - Scroll to zoom - Click to select (shift/cmd for multi-select)
      </div>
    </div>
  );
}
