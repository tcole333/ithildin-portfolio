import { useState } from 'react';

/**
 * Hand-positioned SVG diagram showing the 8 analytical models and their
 * relationships. Positions are editorial — NOT a force-directed graph.
 * Matches the system view from analytical-models.md.
 *
 * Layout:
 *   Top tier:     The Private Order
 *   Middle tier:  Bridge Tax, Narrative Shield, Enabler Gradient
 *   Center:       Manufactured Dependency
 *   Bottom tier:  Complexity as Credential, Jurisdictional Arbitrage, Parallel Financial System
 */

interface ModelNode {
  id: string;
  title: string;
  subtitle: string;
  x: number;
  y: number;
  tier: 'top' | 'middle' | 'center' | 'bottom';
}

interface ModelEdge {
  from: string;
  to: string;
  label: string;
}

const NODES: ModelNode[] = [
  { id: 'private-order', title: 'The Private Order', subtitle: 'access-controlled network', x: 400, y: 50, tier: 'top' },
  { id: 'bridge-tax', title: 'Bridge Tax', subtitle: 'structural holes', x: 140, y: 170, tier: 'middle' },
  { id: 'narrative-shield', title: 'Narrative Shield', subtitle: 'narrative control', x: 400, y: 170, tier: 'middle' },
  { id: 'enabler-gradient', title: 'Enabler Gradient', subtitle: 'complicit → unwitting', x: 660, y: 170, tier: 'middle' },
  { id: 'manufactured-dependency', title: 'Manufactured Dependency', subtitle: 'create problems, sell solutions', x: 400, y: 310, tier: 'center' },
  { id: 'complexity-as-credential', title: 'Complexity as Credential', subtitle: 'the con IS the product', x: 140, y: 440, tier: 'bottom' },
  { id: 'jurisdictional-arbitrage', title: 'Jurisdictional Arbitrage', subtitle: 'multi-jurisdiction exploitation', x: 400, y: 440, tier: 'bottom' },
  { id: 'parallel-financial-system', title: 'Parallel Financial System', subtitle: 'intel-finance shared infra', x: 660, y: 440, tier: 'bottom' },
];

const EDGES: ModelEdge[] = [
  { from: 'private-order', to: 'bridge-tax', label: 'maintained by' },
  { from: 'private-order', to: 'narrative-shield', label: 'operates through' },
  { from: 'private-order', to: 'enabler-gradient', label: 'operates through' },
  { from: 'bridge-tax', to: 'manufactured-dependency', label: 'enables' },
  { from: 'narrative-shield', to: 'manufactured-dependency', label: 'conceals' },
  { from: 'enabler-gradient', to: 'manufactured-dependency', label: 'requires' },
  { from: 'manufactured-dependency', to: 'complexity-as-credential', label: 'operates through' },
  { from: 'manufactured-dependency', to: 'jurisdictional-arbitrage', label: 'operates through' },
  { from: 'manufactured-dependency', to: 'parallel-financial-system', label: 'operates through' },
];

const TIER_COLORS: Record<string, string> = {
  top: '#d1b36a',
  middle: '#8fd3e8',
  center: '#c7d0d9',
  bottom: '#8c97a3',
};

const NODE_W = 180;
const NODE_H = 56;

export default function ModelSystemDiagram() {
  const [hoveredNode, setHoveredNode] = useState<string | null>(null);

  const nodeMap = Object.fromEntries(NODES.map(n => [n.id, n]));

  function getEdgePath(edge: ModelEdge): string {
    const from = nodeMap[edge.from];
    const to = nodeMap[edge.to];
    if (!from || !to) return '';

    const x1 = from.x;
    const y1 = from.y + NODE_H / 2;
    const x2 = to.x;
    const y2 = to.y - NODE_H / 2;
    const cy = (y1 + y2) / 2;

    return `M${x1},${y1} C${x1},${cy} ${x2},${cy} ${x2},${y2}`;
  }

  function isHighlighted(nodeId: string): boolean {
    if (!hoveredNode) return true;
    if (nodeId === hoveredNode) return true;
    return EDGES.some(
      e => (e.from === hoveredNode && e.to === nodeId) ||
           (e.to === hoveredNode && e.from === nodeId)
    );
  }

  function isEdgeHighlighted(edge: ModelEdge): boolean {
    if (!hoveredNode) return true;
    return edge.from === hoveredNode || edge.to === hoveredNode;
  }

  return (
    <div style={{ width: '100%', overflow: 'auto' }}>
      <svg
        viewBox="0 0 800 510"
        style={{
          width: '100%',
          maxWidth: '800px',
          height: 'auto',
          fontFamily: '"Space Grotesk", sans-serif',
        }}
      >
        <defs>
          <marker
            id="arrowhead"
            markerWidth="8"
            markerHeight="6"
            refX="8"
            refY="3"
            orient="auto"
          >
            <polygon points="0 0, 8 3, 0 6" fill="#8c97a3" fillOpacity="0.6" />
          </marker>
        </defs>

        {/* Edges */}
        {EDGES.map((edge, i) => {
          const highlighted = isEdgeHighlighted(edge);
          return (
            <g key={`edge-${i}`}>
              <path
                d={getEdgePath(edge)}
                fill="none"
                stroke="#8c97a3"
                strokeWidth={highlighted ? 1.5 : 0.5}
                strokeOpacity={highlighted ? 0.5 : 0.15}
                markerEnd="url(#arrowhead)"
              />
              {highlighted && hoveredNode && (
                <text
                  x={(nodeMap[edge.from].x + nodeMap[edge.to].x) / 2}
                  y={(nodeMap[edge.from].y + NODE_H / 2 + nodeMap[edge.to].y - NODE_H / 2) / 2}
                  textAnchor="middle"
                  fontSize="9"
                  fill="#8c97a3"
                  fillOpacity="0.8"
                >
                  {edge.label}
                </text>
              )}
            </g>
          );
        })}

        {/* Nodes */}
        {NODES.map(node => {
          const highlighted = isHighlighted(node.id);
          const color = TIER_COLORS[node.tier];

          return (
            <g
              key={node.id}
              transform={`translate(${node.x - NODE_W / 2}, ${node.y - NODE_H / 2})`}
              onMouseEnter={() => setHoveredNode(node.id)}
              onMouseLeave={() => setHoveredNode(null)}
              style={{ cursor: 'pointer' }}
              opacity={highlighted ? 1 : 0.3}
            >
              <a href={`/models/${node.id}`}>
                <rect
                  width={NODE_W}
                  height={NODE_H}
                  rx={3}
                  fill="#12151b"
                  stroke={color}
                  strokeWidth={hoveredNode === node.id ? 1.5 : 0.8}
                  strokeOpacity={hoveredNode === node.id ? 0.9 : 0.5}
                />
                <text
                  x={NODE_W / 2}
                  y={22}
                  textAnchor="middle"
                  fontSize="11"
                  fontWeight="500"
                  fill={color}
                >
                  {node.title}
                </text>
                <text
                  x={NODE_W / 2}
                  y={40}
                  textAnchor="middle"
                  fontSize="9"
                  fill="#8c97a3"
                >
                  {node.subtitle}
                </text>
              </a>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
