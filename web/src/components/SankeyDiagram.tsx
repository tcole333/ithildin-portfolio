import { useEffect, useRef, useState } from 'react';
import * as d3 from 'd3';
import { sankey, sankeyLinkHorizontal, sankeyCenter } from 'd3-sankey';

interface FlowNode {
  id: string;
  name: string;
  category?: string;
}

interface FlowLink {
  source: string;
  target: string;
  value: number;
  label?: string;
}

interface FlowData {
  title: string;
  subtitle?: string;
  nodes: FlowNode[];
  links: FlowLink[];
}

interface Props {
  data: FlowData;
  height?: number;
  valueScale?: 'linear' | 'sqrt' | 'log';
}

const categoryColors: Record<string, string> = {
  person: '#8fd3e8',
  trust: '#d1b36a',
  entity: '#7ea7c1',
  company: '#8fa6b8',
  property: '#b7b1a3',
  nonprofit: '#9aa6b2',
};

function formatAmount(value: number): string {
  if (value >= 1_000_000_000) return `$${(value / 1_000_000_000).toFixed(1)}B`;
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `$${(value / 1_000).toFixed(0)}K`;
  if (value <= 1) return ''; // non-monetary links
  return `$${value.toLocaleString()}`;
}

function toLayoutValue(value: number, mode: Props['valueScale']): number {
  const safe = Math.max(value, 1);
  if (mode === 'sqrt') return Math.sqrt(safe);
  if (mode === 'log') return Math.log10(safe + 1) * 100;
  return safe;
}

export default function SankeyDiagram({ data, height = 500, valueScale = 'linear' }: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const update = () => {
      const rect = container.getBoundingClientRect();
      const nextWidth = Math.floor(rect.width);
      if (nextWidth) setWidth(nextWidth);
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

  useEffect(() => {
    if (!svgRef.current || data.nodes.length === 0 || !width) return;

    const horizontalMargin = Math.max(80, Math.min(160, Math.floor(width * 0.2)));
    const margin = { top: 20, right: horizontalMargin, bottom: 20, left: horizontalMargin };
    const isCompact = width < 820;
    const nodePadding = data.nodes.length <= 12 ? 28 : 16;
    const nodeWidth = data.nodes.length <= 12 ? 18 : 12;
    const labelMax = isCompact ? 16 : 22;
    const labelFontSize = isCompact ? 11 : 12;

    d3.select(svgRef.current).selectAll('*').remove();
    const svg = d3.select(svgRef.current)
      .attr('width', width)
      .attr('height', height)
      .attr('viewBox', `0 0 ${width} ${height}`);

    // Build node index and filter links to only reference existing nodes
    const nodeIds = new Set(data.nodes.map(n => n.id));
    const validLinks = data.links.filter(l => nodeIds.has(l.source) && nodeIds.has(l.target));

    if (validLinks.length === 0) return;

    // Create sankey layout
    const sankeyLayout = sankey()
      .nodeId((d: any) => d.id)
      .nodeWidth(nodeWidth)
      .nodePadding(nodePadding)
      .nodeAlign(sankeyCenter)
      .extent([[margin.left, margin.top], [width - margin.right, height - margin.bottom]]);

    const sankeyData = sankeyLayout({
      nodes: data.nodes.map(d => ({ ...d })),
      links: validLinks.map(d => ({
        source: d.source,
        target: d.target,
        value: toLayoutValue(Math.max(d.value, 1), valueScale),
        originalValue: d.value,
        label: d.label,
      })),
    } as any);

    // Links
    const linkGroup = svg.append('g').attr('fill', 'none');

    linkGroup.selectAll('path')
      .data(sankeyData.links)
      .join('path')
      .attr('d', sankeyLinkHorizontal())
      .attr('stroke', (d: any) => {
        const sourceNode = d.source as any;
        return categoryColors[sourceNode.category] || '#6b7280';
      })
      .attr('stroke-opacity', 0.2)
      .attr('stroke-width', (d: any) => Math.max(2, d.width))
      .attr('stroke-linecap', 'round')
      .on('mouseover', function (event: any, d: any) {
        const originalValue = typeof d.originalValue === 'number' ? d.originalValue : d.value;
        d3.select(this).attr('stroke-opacity', 0.55);
        tooltip.style('visibility', 'visible')
          .html(`<strong>${d.source.name} &rarr; ${d.target.name}</strong>${d.label ? `<br/>${d.label}` : ''}${originalValue > 1 ? `<br/>${formatAmount(originalValue)}` : ''}`);
      })
      .on('mousemove', (event: any) => {
        tooltip.style('top', (event.pageY - 10) + 'px').style('left', (event.pageX + 10) + 'px');
      })
      .on('mouseout', function () {
        d3.select(this).attr('stroke-opacity', 0.2);
        tooltip.style('visibility', 'hidden');
      });

    // Link labels
    const linkLabelData = isCompact
      ? []
      : sankeyData.links.filter((d: any) => {
        const originalValue = typeof d.originalValue === 'number' ? d.originalValue : d.value;
        return originalValue > 1 && d.label && d.width > 8;
      });

    linkGroup.selectAll('text')
      .data(linkLabelData)
      .join('text')
      .attr('x', (d: any) => ((d.source as any).x1 + (d.target as any).x0) / 2)
      .attr('y', (d: any) => (d.y0 + d.y1) / 2)
      .attr('text-anchor', 'middle')
      .attr('dominant-baseline', 'middle')
      .attr('fill', '#c7d0d9')
      .attr('font-size', '10px')
      .style('paint-order', 'stroke')
      .style('stroke', '#0b0d10')
      .style('stroke-width', '3px')
      .style('stroke-linejoin', 'round')
      .style('pointer-events', 'none')
      .text((d: any) => {
        const originalValue = typeof d.originalValue === 'number' ? d.originalValue : d.value;
        return d.label || formatAmount(originalValue);
      });

    // Nodes
    const nodeGroup = svg.append('g');

    nodeGroup.selectAll('rect')
      .data(sankeyData.nodes)
      .join('rect')
      .attr('x', (d: any) => d.x0)
      .attr('y', (d: any) => d.y0)
      .attr('width', (d: any) => d.x1 - d.x0)
      .attr('height', (d: any) => Math.max(1, d.y1 - d.y0))
      .attr('fill', (d: any) => categoryColors[d.category] || '#6b7280')
      .attr('fill-opacity', 0.9)
      .attr('stroke', '#2a313b')
      .attr('stroke-width', 1)
      .attr('rx', 3);

    // Node labels
    nodeGroup.selectAll('text')
      .data(sankeyData.nodes)
      .join('text')
      .attr('x', (d: any) => d.x0 < width / 2 ? d.x0 - 8 : d.x1 + 8)
      .attr('y', (d: any) => (d.y0 + d.y1) / 2)
      .attr('text-anchor', (d: any) => d.x0 < width / 2 ? 'end' : 'start')
      .attr('dominant-baseline', 'middle')
      .attr('fill', '#c7d0d9')
      .attr('font-size', `${labelFontSize}px`)
      .attr('font-weight', '500')
      .style('paint-order', 'stroke')
      .style('stroke', '#0b0d10')
      .style('stroke-width', '3px')
      .style('stroke-linejoin', 'round')
      .text((d: any) => {
        const name = d.name || '';
        if (name.length <= labelMax) return name;
        return `${name.slice(0, Math.max(0, labelMax - 3))}...`;
      });

    // Tooltip
    const tooltip = d3.select('body')
      .append('div')
      .style('position', 'absolute')
      .style('visibility', 'hidden')
      .style('background', '#12151b')
      .style('color', '#c7d0d9')
      .style('border', '1px solid #2a313b')
      .style('padding', '8px 12px')
      .style('border-radius', '6px')
      .style('font-size', '12px')
      .style('pointer-events', 'none')
      .style('z-index', '1000');

    return () => {
      tooltip.remove();
    };
  }, [data, height, width, valueScale]);

  return (
    <div ref={containerRef} className="surface p-4">
      <div className="mb-4">
        <h3 className="text-lg font-semibold text-moon">{data.title}</h3>
        {data.subtitle && <p className="text-sm text-mithril mt-1">{data.subtitle}</p>}
      </div>
      <svg ref={svgRef} className="w-full graph-canvas" style={{ height: `${height}px` }} />
    </div>
  );
}
