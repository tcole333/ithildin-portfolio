import { useState, useMemo } from 'react';
import InvestigationFilter, { type Investigation } from './InvestigationFilter';

interface DossierEntry {
  name: string;
  slug: string;
  profile_ids: string[];
  stats: {
    total_findings: number;
    total_connections: number;
    total_entities: number;
    finding_types?: Record<string, number>;
  };
  last_updated?: string;
}

interface Props {
  dossiers: DossierEntry[];
  investigations: Investigation[];
}

export default function DossierIndex({ dossiers, investigations }: Props) {
  const [filter, setFilter] = useState<string | null>(() => {
    if (typeof window === 'undefined') return null;
    const params = new URLSearchParams(window.location.search);
    return params.get('inv') || null;
  });

  const filtered = useMemo(() => {
    if (!filter) return dossiers;
    return dossiers.filter(d => d.profile_ids?.includes(filter));
  }, [dossiers, filter]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const inv of investigations) {
      c[inv.id] = dossiers.filter(d => d.profile_ids?.includes(inv.id)).length;
    }
    return c;
  }, [dossiers, investigations]);

  return (
    <div className="space-y-6">
      <div className="surface p-4">
        <InvestigationFilter
          investigations={investigations}
          onFilterChange={setFilter}
          counts={counts}
          totalCount={dossiers.length}
        />
      </div>

      <div className="flex items-center justify-between">
        <div className="text-xs text-mithril font-mono">
          {filtered.length} {filtered.length === 1 ? 'dossier' : 'dossiers'}
          {filter && ` in ${investigations.find(i => i.id === filter)?.name || filter}`}
        </div>
      </div>

      <div className="grid gap-3">
        {filtered.map((d, idx) => (
          <a
            key={d.slug}
            href={`/dossiers/${d.slug}`}
            className="surface p-5 transition reveal"
            style={{ '--delay': `${Math.min(idx * 0.02, 0.4)}s` } as React.CSSProperties}
          >
            <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
              <div className="space-y-2">
                <div className="flex items-center gap-3">
                  <span className="text-moon font-semibold">{d.name}</span>
                  {d.profile_ids?.length > 0 && (
                    <span className="flex gap-1">
                      {d.profile_ids.map(pid => {
                        const inv = investigations.find(i => i.id === pid);
                        return inv ? (
                          <span
                            key={pid}
                            className="inv-dot"
                            style={{ backgroundColor: inv.color }}
                            title={inv.name}
                          />
                        ) : null;
                      })}
                    </span>
                  )}
                </div>
                {d.stats.finding_types && (
                  <div className="flex flex-wrap gap-2 text-xs text-mithril">
                    {Object.entries(d.stats.finding_types).slice(0, 4).map(([type, count]) => (
                      <span key={type} className="chip" style={{ letterSpacing: '0.14em' }}>{type}: {count}</span>
                    ))}
                  </div>
                )}
              </div>
              <div className="flex flex-wrap gap-4 text-xs text-mithril font-mono">
                <span>{d.stats.total_findings} findings</span>
                <span>{d.stats.total_connections} connections</span>
                <span>{d.stats.total_entities} entities</span>
              </div>
            </div>
          </a>
        ))}
      </div>
    </div>
  );
}
