import { useEffect, useRef, useState } from 'react';

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
  info_loss?: string;
}

interface FlowData {
  title: string;
  subtitle?: string;
  nodes: FlowNode[];
  links: FlowLink[];
}

interface Props {
  data: FlowData;
}

const C = {
  void: '#0b0d10',
  stone: '#12151b',
  slate: '#1c222b',
  ash: '#2a313b',
  mithril: '#8c97a3',
  moonlight: '#c7d0d9',
  icy: '#8fd3e8',
  ember: '#d1b36a',
  red: '#cf6679',
};

const categoryColors: Record<string, string> = {
  person: C.icy,
  trust: C.ember,
  entity: '#7ea7c1',
  bank: '#9aa6b2',
  registry: '#b7b1a3',
  question: C.moonlight,
};

export default function StepDiagram({ data }: Props) {
  // Sort nodes based on links to form a sequence
  const sequence: { node: FlowNode; linkToNext?: FlowLink }[] = [];
  
  if (data.nodes.length > 0 && data.links.length > 0) {
    let currentId = data.links[0].source;
    while (currentId) {
      const node = data.nodes.find(n => n.id === currentId);
      if (!node) break;
      
      const linkToNext = data.links.find(l => l.source === currentId);
      sequence.push({ node, linkToNext });
      
      currentId = linkToNext ? linkToNext.target : '';
      if (!currentId) {
        // Last node might not have outbound links but we still want to render it
        const lastNode = data.nodes.find(n => n.id === linkToNext?.target);
        if (lastNode) {
          sequence.push({ node: lastNode });
        }
      }
    }
  }

  return (
    <div className="surface p-6 font-ui bg-[rgba(18,21,27,0.5)] border border-[color:var(--color-ash)] rounded-lg">
      <div className="mb-8">
        <h3 className="text-xl font-display text-moon font-semibold">{data.title}</h3>
        {data.subtitle && <p className="text-sm text-mithril mt-2 font-body">{data.subtitle}</p>}
      </div>

      <div className="relative pl-6 ml-2 border-l border-[color:var(--color-ash)] space-y-6">
        {sequence.map((step, idx) => {
          const color = categoryColors[step.node.category || ''] || C.mithril;
          const isLast = idx === sequence.length - 1;
          
          return (
            <div key={step.node.id} className="relative">
              {/* Node indicator */}
              <div 
                className="absolute -left-[31px] top-4 w-3 h-3 rounded-full border-[2px] border-[color:var(--color-stone)]"
                style={{ backgroundColor: color }}
              />
              
              <div className="bg-[rgba(28,34,43,0.5)] border border-[color:var(--color-ash)] p-4 rounded-md inline-block min-w-[280px] max-w-full relative">
                <div 
                  className="text-[0.65rem] uppercase tracking-widest font-mono mb-1 font-bold" 
                  style={{ color }}
                >
                  {step.node.category || 'Step'}
                </div>
                <div className="text-moonlight font-semibold text-lg">{step.node.name}</div>
              </div>
              
              {/* Link annotation */}
              {step.linkToNext && (
                <div className="mt-3 mb-5 pl-2 max-w-2xl">
                  {step.linkToNext.label && (
                    <div className="text-sm text-moonlight mb-2 border-l-[3px] pl-3 border-[color:var(--color-icy)] ml-2 leading-relaxed opacity-90">
                      {step.linkToNext.label}
                    </div>
                  )}
                  {step.linkToNext.info_loss && (
                    <div className="text-[0.7rem] text-mithril font-mono mt-3 ml-2 flex items-start gap-2 bg-[rgba(207,102,121,0.08)] p-3 rounded-md border border-[rgba(207,102,121,0.15)]">
                      <span className="text-[color:var(--color-red)] font-bold text-base leading-none opacity-80 mt-[-1px]">↳</span>
                      <span className="leading-relaxed opacity-90">
                        <strong className="text-[color:var(--color-ember)] mr-1 tracking-wider">DEGRADATION:</strong>
                        {step.linkToNext.info_loss}
                      </span>
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}