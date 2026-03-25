import { useState, useMemo } from 'react';
import InvestigationFilter, { type Investigation } from './InvestigationFilter';
import NetworkGraph from './NetworkGraph';

interface NetworkData {
  nodes: any[];
  edges: any[];
  stats: {
    total_nodes: number;
    person_nodes: number;
    entity_nodes: number;
    total_edges: number;
  };
}

interface Props {
  data: NetworkData;
  dossierSlugs: string[];
  investigations: Investigation[];
}

export default function NetworkPage({ data, dossierSlugs, investigations }: Props) {
  const [filter, setFilter] = useState<string | null>(() => {
    if (typeof window === 'undefined') return null;
    const params = new URLSearchParams(window.location.search);
    return params.get('inv') || null;
  });

  const filteredData = useMemo(() => {
    if (!filter) return data;

    // Filter edges: keep edges that belong to the selected investigation
    // or have no profile_ids (structural edges like entity roles)
    const filteredEdges = data.edges.filter(edge => {
      const pids = edge.profile_ids as string[] | undefined;
      if (!pids || pids.length === 0) return true;
      return pids.includes(filter);
    });

    // Keep nodes that have at least one visible edge
    const visibleNodeIds = new Set<string>();
    for (const edge of filteredEdges) {
      const sid = typeof edge.source === 'string' ? edge.source : edge.source.id;
      const tid = typeof edge.target === 'string' ? edge.target : edge.target.id;
      visibleNodeIds.add(sid);
      visibleNodeIds.add(tid);
    }

    const filteredNodes = data.nodes.filter(n => visibleNodeIds.has(n.id));

    return {
      nodes: filteredNodes,
      edges: filteredEdges,
      stats: {
        total_nodes: filteredNodes.length,
        person_nodes: filteredNodes.filter((n: any) => n.type === 'person').length,
        entity_nodes: filteredNodes.filter((n: any) => n.type === 'entity').length,
        total_edges: filteredEdges.length,
      },
    };
  }, [data, filter]);

  const edgeCounts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const inv of investigations) {
      c[inv.id] = data.edges.filter(e => {
        const pids = e.profile_ids as string[] | undefined;
        return pids?.includes(inv.id);
      }).length;
    }
    return c;
  }, [data, investigations]);

  return (
    <div className="space-y-4">
      {investigations.length > 0 && (
        <div className="surface p-4">
          <InvestigationFilter
            investigations={investigations}
            onFilterChange={setFilter}
            counts={edgeCounts}
            totalCount={data.edges.length}
          />
        </div>
      )}
      <div className="surface p-4">
        <div className="h-[70vh] md:h-[75vh] rounded-sm overflow-hidden">
          <NetworkGraph data={filteredData} dossierSlugs={dossierSlugs} />
        </div>
      </div>
    </div>
  );
}
