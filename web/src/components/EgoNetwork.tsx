import { useEffect, useMemo, useRef, useState } from 'react';
import * as d3 from 'd3';

interface Connection {
  target: string;
  type: string;
  strength: number;
  evidence_ref?: string | null;
  description?: string | null;
  verified?: boolean;
  certainty?: 'documented' | 'inferred' | 'alleged';
}

interface Props {
  center: string;
  connections: Connection[];
  secondHop?: Record<string, Connection[]>;
  depth?: 1 | 2;
  height?: number;
}

type Certainty = 'documented' | 'inferred' | 'alleged';

type GraphNode = {
  id: string;
  hop: number;
  connectionCount: number;
  strengthToCenter: number;
  anchorX?: number;
  anchorY?: number;
  x?: number;
  y?: number;
  fx?: number | null;
  fy?: number | null;
};

type GraphEdge = {
  source: string | GraphNode;
  target: string | GraphNode;
  type: string;
  strength: number;
  certainty: Certainty;
  evidence_ref?: string | null;
  description?: string | null;
};

const C = {
  void: '#0b0d10',
  stone: '#12151b',
  slate: '#1c222b',
  ash: '#2a313b',
  mithril: '#8c97a3',
  moonlight: '#c7d0d9',
  icy: '#8fd3e8',
  ember: '#d1b36a',
};

const FONT = {
  ui: '"Space Grotesk", sans-serif',
  mono: '"IBM Plex Mono", monospace',
};

const RELATIONSHIP_COLORS: Record<string, string> = {
  financial: C.ember,
  legal: '#b7b1a3',
  employment: '#8fa6b8',
  social: C.icy,
  corporate: '#7ea7c1',
  intelligence: '#a09c8a',
  advisory: '#9aa6b2',
};

const CERTAINTY_DASH: Record<Certainty, string> = {
  documented: '0',
  inferred: '4 3',
  alleged: '2 3',
};

function relationshipColor(type: string): string {
  return RELATIONSHIP_COLORS[type] || C.mithril;
}

function inferCertainty(connection: Connection): Certainty {
  if (connection.certainty) return connection.certainty;
  if (connection.verified === true || connection.evidence_ref) return 'documented';

  const text = `${connection.description || ''}`.toLowerCase();
  if (text.includes('alleg') || text.includes('possible') || text.includes('unverified') || text.includes('rumor')) {
    return 'alleged';
  }
  return 'inferred';
}

function edgeNodeId(endpoint: string | GraphNode): string {
  return typeof endpoint === 'string' ? endpoint : endpoint.id;
}

function formatStrength(strength: number): string {
  return `${Math.round(strength * 100)}%`;
}

export default function EgoNetwork({ center, connections, secondHop = {}, depth = 1, height = 500 }: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const chartHostRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);

  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [layoutMode, setLayoutMode] = useState<'scaffolded' | 'force'>('scaffolded');
  const [showSecondHop, setShowSecondHop] = useState(depth === 2);
  const [documentedOnly, setDocumentedOnly] = useState(false);
  const [activeTypes, setActiveTypes] = useState<string[]>([]);

  useEffect(() => {
    setSelectedNodeId(null);
  }, [center]);

  useEffect(() => {
    setShowSecondHop(depth === 2);
  }, [depth]);

  const relationshipStats = useMemo(() => {
    const counts = new Map<string, number>();
    connections.forEach(connection => {
      counts.set(connection.type, (counts.get(connection.type) || 0) + 1);
    });

    if (depth === 2) {
      Object.values(secondHop).forEach(edges => {
        edges.forEach(connection => {
          counts.set(connection.type, (counts.get(connection.type) || 0) + 1);
        });
      });
    }

    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);
  }, [connections, secondHop, depth]);

  useEffect(() => {
    setActiveTypes(prev => {
      const available = relationshipStats.map(([type]) => type);
      if (available.length === 0) return [];
      if (prev.length === 0) return available;

      const availableSet = new Set(available);
      const retained = prev.filter(type => availableSet.has(type));
      const missing = available.filter(type => !retained.includes(type));
      return [...retained, ...missing];
    });
  }, [relationshipStats]);

  const graph = useMemo(() => {
    const activeTypeSet = new Set(activeTypes);
    const nodeMap = new Map<string, GraphNode>();
    const edgeMap = new Map<string, GraphEdge>();

    nodeMap.set(center, {
      id: center,
      hop: 0,
      connectionCount: 0,
      strengthToCenter: 1,
    });

    const addNode = (id: string, hopHint: number, strengthToCenter = 0) => {
      const existing = nodeMap.get(id);
      if (existing) {
        existing.hop = Math.min(existing.hop, hopHint);
        existing.strengthToCenter = Math.max(existing.strengthToCenter, strengthToCenter);
        return existing;
      }

      const node: GraphNode = {
        id,
        hop: hopHint,
        connectionCount: 0,
        strengthToCenter,
      };
      nodeMap.set(id, node);
      return node;
    };

    const addEdge = (source: string, connection: Connection, hopHint: number) => {
      if (!connection.target || connection.target === source) return;
      if (activeTypeSet.size > 0 && !activeTypeSet.has(connection.type)) return;

      const certainty = inferCertainty(connection);
      if (documentedOnly && certainty !== 'documented') return;

      addNode(source, source === center ? 0 : hopHint - 1);
      addNode(connection.target, hopHint, source === center ? connection.strength : 0);

      const key = `${source}::${connection.target}::${connection.type}`;
      const existing = edgeMap.get(key);
      if (!existing || existing.strength < connection.strength) {
        edgeMap.set(key, {
          source,
          target: connection.target,
          type: connection.type,
          strength: connection.strength,
          certainty,
          evidence_ref: connection.evidence_ref,
          description: connection.description,
        });
      }
    };

    const primary = [...connections].sort((a, b) => b.strength - a.strength).slice(0, 36);
    primary.forEach(connection => addEdge(center, connection, 1));

    if (depth === 2 && showSecondHop) {
      primary.forEach(connection => {
        const sourceId = connection.target;
        const candidateEdges = [...(secondHop[sourceId] || [])]
          .filter(item => item.target !== center)
          .sort((a, b) => b.strength - a.strength)
          .slice(0, 4);

        candidateEdges.forEach(item => addEdge(sourceId, item, 2));
      });
    }

    const edges = Array.from(edgeMap.values());
    const adjacency = new Map<string, Set<string>>();

    edges.forEach(edge => {
      const sourceId = edgeNodeId(edge.source);
      const targetId = edgeNodeId(edge.target);
      if (!adjacency.has(sourceId)) adjacency.set(sourceId, new Set());
      if (!adjacency.has(targetId)) adjacency.set(targetId, new Set());
      adjacency.get(sourceId)!.add(targetId);
      adjacency.get(targetId)!.add(sourceId);
    });

    const hops = new Map<string, number>();
    const queue: string[] = [center];
    hops.set(center, 0);

    while (queue.length > 0) {
      const current = queue.shift()!;
      const currentHop = hops.get(current) || 0;
      const neighbors = adjacency.get(current);
      if (!neighbors) continue;
      neighbors.forEach(neighbor => {
        if (!hops.has(neighbor)) {
          hops.set(neighbor, currentHop + 1);
          queue.push(neighbor);
        }
      });
    }

    const reachable = new Set(hops.keys());
    reachable.add(center);

    const reachableEdges = edges.filter(edge => {
      const sourceId = edgeNodeId(edge.source);
      const targetId = edgeNodeId(edge.target);
      return reachable.has(sourceId) && reachable.has(targetId);
    });

    reachableEdges.forEach(edge => {
      const sourceId = edgeNodeId(edge.source);
      const targetId = edgeNodeId(edge.target);
      const sourceNode = nodeMap.get(sourceId);
      const targetNode = nodeMap.get(targetId);
      if (sourceNode) sourceNode.connectionCount += 1;
      if (targetNode) targetNode.connectionCount += 1;
    });

    const nodes = Array.from(nodeMap.values())
      .filter(node => reachable.has(node.id))
      .map(node => ({
        ...node,
        hop: hops.get(node.id) ?? node.hop,
      }));

    return {
      nodes,
      edges: reachableEdges,
      adjacency,
    };
  }, [center, connections, secondHop, depth, showSecondHop, activeTypes, documentedOnly]);

  useEffect(() => {
    if (!selectedNodeId) return;
    if (graph.nodes.some(node => node.id === selectedNodeId)) return;
    setSelectedNodeId(center);
  }, [graph.nodes, selectedNodeId, center]);

  useEffect(() => {
    const host = chartHostRef.current;
    if (!host) return;

    const update = () => {
      const rect = host.getBoundingClientRect();
      const nextWidth = Math.floor(rect.width);
      if (nextWidth) setWidth(nextWidth);
    };

    update();

    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', update);
      return () => window.removeEventListener('resize', update);
    }

    const observer = new ResizeObserver(update);
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!svgRef.current || !width || graph.nodes.length === 0) return;

    d3.select(svgRef.current).selectAll('*').remove();

    const svg = d3.select(svgRef.current)
      .attr('width', width)
      .attr('height', height)
      .attr('viewBox', `0 0 ${width} ${height}`);

    const g = svg.append('g');

    const zoom = d3.zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.35, 5])
      .filter((event: any) => {
        if (event.type === 'wheel') return event.ctrlKey || event.metaKey;
        if (event.type === 'dblclick') return false;
        return !event.button;
      })
      .on('zoom', event => g.attr('transform', event.transform));

    svg.call(zoom as any);

    const nodes = graph.nodes.map(node => ({ ...node }));
    const edges = graph.edges.map(edge => ({ ...edge }));

    const centerX = width / 2;
    const centerY = height / 2;
    const ring1 = Math.max(110, Math.min(width, height) * 0.28);
    const ring2 = Math.max(ring1 + 70, Math.min(width, height) * 0.42);

    if (layoutMode === 'scaffolded') {
      const firstRing = nodes
        .filter(node => node.hop === 1)
        .sort((a, b) => b.strengthToCenter - a.strengthToCenter || a.id.localeCompare(b.id));

      const secondRing = nodes
        .filter(node => node.hop >= 2)
        .sort((a, b) => b.connectionCount - a.connectionCount || a.id.localeCompare(b.id));

      const placeRing = (ringNodes: GraphNode[], radius: number) => {
        if (ringNodes.length === 0) return;
        ringNodes.forEach((node, index) => {
          const angle = -Math.PI / 2 + (index / ringNodes.length) * Math.PI * 2;
          node.anchorX = centerX + radius * Math.cos(angle);
          node.anchorY = centerY + radius * Math.sin(angle);
          node.x = node.anchorX;
          node.y = node.anchorY;
        });
      };

      placeRing(firstRing, ring1);
      placeRing(secondRing, ring2);

      const centerNode = nodes.find(node => node.id === center);
      if (centerNode) {
        centerNode.anchorX = centerX;
        centerNode.anchorY = centerY;
        centerNode.x = centerX;
        centerNode.y = centerY;
        centerNode.fx = centerX;
        centerNode.fy = centerY;
      }
    }

    const maxConnections = Math.max(...nodes.map(node => node.connectionCount || 1), 1);
    const radiusScale = d3.scaleSqrt().domain([0, maxConnections]).range([8, 30]).clamp(true);

    const simulation = d3.forceSimulation(nodes as any)
      .alphaDecay(layoutMode === 'scaffolded' ? 0.08 : 0.05)
      .force('link', d3.forceLink(edges as any)
        .id((node: any) => node.id)
        .distance((edge: any) => {
          const sourceHop = edge.source?.hop ?? 1;
          const targetHop = edge.target?.hop ?? 1;
          if (Math.min(sourceHop, targetHop) === 0) return layoutMode === 'scaffolded' ? ring1 * 0.6 : 110;
          return layoutMode === 'scaffolded' ? 88 : 70;
        }))
      .force('charge', d3.forceManyBody().strength(layoutMode === 'scaffolded' ? -170 : -280))
      .force('center', d3.forceCenter(centerX, centerY))
      .force('collision', d3.forceCollide().radius((node: any) => radiusScale(node.connectionCount || 1) + 8));

    if (layoutMode === 'scaffolded') {
      simulation
        .force('x', d3.forceX((node: any) => node.anchorX ?? centerX).strength((node: any) => (node.hop === 0 ? 1 : 0.24)))
        .force('y', d3.forceY((node: any) => node.anchorY ?? centerY).strength((node: any) => (node.hop === 0 ? 1 : 0.24)));
    }

    if (layoutMode === 'scaffolded') {
      const guides = g.append('g').style('pointer-events', 'none');

      guides.append('circle')
        .attr('cx', centerX)
        .attr('cy', centerY)
        .attr('r', ring1)
        .attr('fill', 'none')
        .attr('stroke', C.ash)
        .attr('stroke-opacity', 0.4)
        .attr('stroke-dasharray', '3 4');

      guides.append('text')
        .attr('x', centerX)
        .attr('y', centerY - ring1 - 8)
        .attr('text-anchor', 'middle')
        .attr('fill', C.moonlight)
        .attr('opacity', 0.88)
        .attr('font-size', '9px')
        .attr('font-family', FONT.mono)
        .text('1-hop ring');

      if (nodes.some(node => node.hop >= 2)) {
        guides.append('circle')
          .attr('cx', centerX)
          .attr('cy', centerY)
          .attr('r', ring2)
          .attr('fill', 'none')
          .attr('stroke', C.ash)
          .attr('stroke-opacity', 0.25)
          .attr('stroke-dasharray', '2 5');

        guides.append('text')
          .attr('x', centerX)
          .attr('y', centerY - ring2 - 8)
          .attr('text-anchor', 'middle')
          .attr('fill', C.moonlight)
          .attr('opacity', 0.85)
          .attr('font-size', '9px')
          .attr('font-family', FONT.mono)
          .text('2-hop ring');
      }
    }

    const link = g.append('g')
      .selectAll('line')
      .data(edges)
      .join('line')
      .attr('stroke', edge => relationshipColor(edge.type))
      .attr('stroke-width', edge => Math.max(1.1, edge.strength * 2.5))
      .attr('stroke-opacity', 0.38)
      .attr('stroke-dasharray', edge => CERTAINTY_DASH[edge.certainty]);

    const node = g.append('g')
      .selectAll('g')
      .data(nodes)
      .join('g')
      .call(d3.drag<any, any>()
        .on('start', (event, current) => {
          if (!event.active) simulation.alphaTarget(0.25).restart();
          current.fx = current.x;
          current.fy = current.y;
        })
        .on('drag', (event, current) => {
          if (layoutMode === 'scaffolded' && current.id === center) return;
          current.fx = event.x;
          current.fy = event.y;
        })
        .on('end', (event, current) => {
          if (!event.active) simulation.alphaTarget(0);
          if (layoutMode === 'scaffolded' && current.id === center) {
            current.fx = centerX;
            current.fy = centerY;
            return;
          }
          current.fx = null;
          current.fy = null;
        }) as any);

    node.append('circle')
      .attr('class', 'node-halo')
      .attr('r', current => radiusScale(current.connectionCount || 1) + 6)
      .attr('fill', 'none')
      .attr('stroke', C.icy)
      .attr('stroke-opacity', 0)
      .attr('stroke-width', 1.5);

    node.append('circle')
      .attr('class', 'node-core')
      .attr('r', current => {
        const base = radiusScale(current.connectionCount || 1);
        return current.id === center ? base * 1.16 : base;
      })
      .attr('fill', C.stone)
      .attr('stroke', current => (current.id === center ? C.icy : current.hop === 1 ? C.moonlight : C.mithril))
      .attr('stroke-width', current => (current.id === center ? 1.8 : 1.05))
      .style('cursor', 'pointer')
      .on('click', (_event, current) => {
        setSelectedNodeId(previous => (previous === current.id ? null : current.id));
      });

    const adjacency = new Map<string, Set<string>>();
    edges.forEach(edge => {
      const sourceId = edgeNodeId(edge.source);
      const targetId = edgeNodeId(edge.target);
      if (!adjacency.has(sourceId)) adjacency.set(sourceId, new Set());
      if (!adjacency.has(targetId)) adjacency.set(targetId, new Set());
      adjacency.get(sourceId)!.add(targetId);
      adjacency.get(targetId)!.add(sourceId);
    });

    const labelSet = new Set<string>();
    labelSet.add(center);

    nodes
      .slice()
      .sort((a, b) => b.connectionCount - a.connectionCount)
      .slice(0, 8)
      .forEach(item => labelSet.add(item.id));

    if (selectedNodeId) {
      labelSet.add(selectedNodeId);
      (adjacency.get(selectedNodeId) || new Set()).forEach(item => labelSet.add(item));
    }

    const labels = g.append('g')
      .selectAll('text')
      .data(nodes.filter(item => labelSet.has(item.id)))
      .join('text')
      .text(item => (item.id.length > 26 ? `${item.id.slice(0, 24)}…` : item.id))
      .attr('text-anchor', 'middle')
      .attr('fill', '#e3edf7')
      .attr('font-size', item => (item.id === center ? '13px' : '11px'))
      .attr('font-weight', item => (item.id === center ? 600 : 500))
      .attr('font-family', FONT.ui)
      .style('paint-order', 'stroke')
      .style('stroke', C.void)
      .style('stroke-width', '4px')
      .style('stroke-linejoin', 'round')
      .style('pointer-events', 'none');

    const tooltip = d3.select('body')
      .append('div')
      .style('position', 'absolute')
      .style('visibility', 'hidden')
      .style('background', C.stone)
      .style('color', C.moonlight)
      .style('border', `1px solid ${C.ash}`)
      .style('padding', '8px 12px')
      .style('border-radius', '6px')
      .style('font-size', '12px')
      .style('font-family', FONT.ui)
      .style('pointer-events', 'none')
      .style('z-index', '1000')
      .style('max-width', '320px');

    const applyState = (hoveredNodeId: string | null) => {
      const focusId = hoveredNodeId ?? selectedNodeId;
      const neighbors = focusId ? adjacency.get(focusId) || new Set<string>() : new Set<string>();

      node.select<SVGCircleElement>('circle.node-core')
        .attr('fill-opacity', current => {
          if (!focusId) return 0.95;
          if (current.id === focusId) return 1;
          if (neighbors.has(current.id)) return 0.9;
          return 0.5;
        });

      node.select<SVGCircleElement>('circle.node-halo')
        .attr('stroke-opacity', current => (focusId && current.id === focusId ? 0.88 : 0));

      link
        .attr('stroke-opacity', current => {
          if (!focusId) return 0.38;
          const sourceId = edgeNodeId(current.source);
          const targetId = edgeNodeId(current.target);
          if (sourceId === focusId || targetId === focusId) return 0.88;
          return 0.24;
        });

      labels.attr('opacity', current => {
        if (!focusId) return 1;
        if (current.id === focusId || neighbors.has(current.id)) return 1;
        return 0.62;
      });
    };

    node
      .on('mouseover', (_event, current) => {
        applyState(current.id);
        tooltip.style('visibility', 'visible').html([
          `<strong>${current.id}</strong>`,
          `${current.connectionCount} connections`,
          `Hop: ${current.hop}`,
        ].join('<br/>'));
      })
      .on('mousemove', event => {
        tooltip
          .style('top', `${event.pageY - 10}px`)
          .style('left', `${event.pageX + 10}px`);
      })
      .on('mouseout', () => {
        tooltip.style('visibility', 'hidden');
        applyState(null);
      });

    link
      .on('mouseover', function (_event, current) {
        const sourceId = edgeNodeId(current.source);
        const targetId = edgeNodeId(current.target);
        d3.select(this).attr('stroke-opacity', 0.95);
        tooltip.style('visibility', 'visible').html([
          `<strong>${sourceId} ↔ ${targetId}</strong>`,
          `Type: ${current.type}`,
          `Strength: ${formatStrength(current.strength)}`,
          `Certainty: ${current.certainty}`,
          current.description || '',
          current.evidence_ref ? `Ref: ${current.evidence_ref}` : '',
        ].filter(Boolean).join('<br/>'));
      })
      .on('mousemove', event => {
        tooltip
          .style('top', `${event.pageY - 10}px`)
          .style('left', `${event.pageX + 10}px`);
      })
      .on('mouseout', function () {
        tooltip.style('visibility', 'hidden');
        applyState(null);
      });

    simulation.on('tick', () => {
      link
        .attr('x1', current => (current.source as GraphNode).x || 0)
        .attr('y1', current => (current.source as GraphNode).y || 0)
        .attr('x2', current => (current.target as GraphNode).x || 0)
        .attr('y2', current => (current.target as GraphNode).y || 0);

      node.attr('transform', current => `translate(${current.x || 0},${current.y || 0})`);

      labels
        .attr('x', current => current.x || 0)
        .attr('y', current => (current.y || 0) + radiusScale(current.connectionCount || 1) + 12);
    });

    applyState(null);

    return () => {
      simulation.stop();
      tooltip.remove();
    };
  }, [graph, width, height, center, layoutMode, selectedNodeId]);

  const hasSecondHopData = depth === 2 && Object.keys(secondHop).length > 0;

  return (
    <div className="surface p-4">
      <div className="flex items-start justify-between gap-4 mb-3">
        <div>
          <h3 className="text-lg font-semibold text-moon">{center}</h3>
          <div className="text-xs text-mithril font-mono mt-1">
            {graph.nodes.length} nodes · {graph.edges.length} edges · {showSecondHop && hasSecondHopData ? '2-hop' : '1-hop'}
          </div>
          <div className="text-[0.65rem] text-mithril font-mono mt-1 uppercase tracking-[0.2em]">
            Size = connections · color = relationship · dash = certainty
          </div>
        </div>

        <div className="text-xs text-mithril font-mono">
          {layoutMode === 'scaffolded' ? 'Scaffolded layout' : 'Force layout'}
        </div>
      </div>

      <div className="flex flex-wrap gap-2 mb-2">
        <button
          type="button"
          onClick={() => setLayoutMode('scaffolded')}
          className={`rounded border px-2.5 py-1 text-xs transition ${layoutMode === 'scaffolded'
            ? 'border-[color:var(--color-icy)] text-moon bg-[rgba(18,21,27,0.9)]'
            : 'border-[color:var(--color-ash)] text-mithril bg-[rgba(18,21,27,0.55)]'}`}
        >
          Scaffolded rings
        </button>

        <button
          type="button"
          onClick={() => setLayoutMode('force')}
          className={`rounded border px-2.5 py-1 text-xs transition ${layoutMode === 'force'
            ? 'border-[color:var(--color-icy)] text-moon bg-[rgba(18,21,27,0.9)]'
            : 'border-[color:var(--color-ash)] text-mithril bg-[rgba(18,21,27,0.55)]'}`}
        >
          Free force
        </button>

        <label className="inline-flex items-center gap-2 rounded border border-[color:var(--color-ash)] bg-[rgba(18,21,27,0.55)] px-2.5 py-1 text-xs text-mithril">
          <input
            type="checkbox"
            checked={documentedOnly}
            onChange={event => setDocumentedOnly(event.target.checked)}
          />
          Documented only
        </label>

        {hasSecondHopData && (
          <label className="inline-flex items-center gap-2 rounded border border-[color:var(--color-ash)] bg-[rgba(18,21,27,0.55)] px-2.5 py-1 text-xs text-mithril">
            <input
              type="checkbox"
              checked={showSecondHop}
              onChange={event => setShowSecondHop(event.target.checked)}
            />
            Show 2-hop
          </label>
        )}
      </div>

      <div className="flex flex-wrap gap-2 mb-4">
        {relationshipStats.map(([type, count]) => {
          const active = activeTypes.includes(type);
          return (
            <button
              key={type}
              type="button"
              onClick={() => {
                setActiveTypes(previous => {
                  if (previous.includes(type)) {
                    const next = previous.filter(item => item !== type);
                    return next.length ? next : previous;
                  }
                  return [...previous, type];
                });
              }}
              className={`inline-flex items-center gap-1.5 rounded border px-2 py-1 text-xs transition ${active
                ? 'border-[color:var(--color-icy)] text-moon bg-[rgba(18,21,27,0.88)]'
                : 'border-[color:var(--color-ash)] text-mithril bg-[rgba(18,21,27,0.5)]'}`}
            >
              <span className="inline-block w-3 h-0.5" style={{ background: relationshipColor(type) }} />
              {type}
              <span className="text-[10px] text-mithril">({count})</span>
            </button>
          );
        })}
      </div>

      <div ref={chartHostRef} className="surface-muted p-3">
        <svg ref={svgRef} className="w-full graph-canvas" style={{ height: `${height}px` }} />
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-xs text-mithril font-mono">
          <span>Ctrl/Cmd + scroll to zoom · drag nodes to inspect structure</span>
          <span>Solid = documented · dashed = inferred · dotted = alleged</span>
        </div>
      </div>
    </div>
  );
}
