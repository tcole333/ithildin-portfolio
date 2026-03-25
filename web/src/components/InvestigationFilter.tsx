import { useState, useEffect } from 'react';

export interface Investigation {
  id: string;
  name: string;
  description: string;
  color: string;
}

interface Props {
  investigations: Investigation[];
  onFilterChange: (id: string | null) => void;
  counts?: Record<string, number>;
  totalCount?: number;
}

export default function InvestigationFilter({ investigations, onFilterChange, counts, totalCount }: Props) {
  const [active, setActive] = useState<string | null>(() => {
    if (typeof window === 'undefined') return null;
    const params = new URLSearchParams(window.location.search);
    return params.get('inv') || null;
  });

  useEffect(() => {
    onFilterChange(active);
  }, []);

  const handleClick = (id: string | null) => {
    setActive(id);
    onFilterChange(id);

    const url = new URL(window.location.href);
    if (id) {
      url.searchParams.set('inv', id);
    } else {
      url.searchParams.delete('inv');
    }
    window.history.replaceState({}, '', url.toString());
  };

  return (
    <div className="inv-filter">
      <button
        className={`inv-pill ${active === null ? 'inv-pill--active' : ''}`}
        style={{ '--inv-color': 'rgba(143, 211, 232, 0.35)' } as React.CSSProperties}
        onClick={() => handleClick(null)}
      >
        All
        {totalCount != null && <span className="inv-pill__count">{totalCount}</span>}
      </button>
      {investigations.map(inv => (
        <button
          key={inv.id}
          className={`inv-pill ${active === inv.id ? 'inv-pill--active' : ''}`}
          style={{ '--inv-color': inv.color } as React.CSSProperties}
          onClick={() => handleClick(inv.id)}
        >
          {inv.name}
          {counts?.[inv.id] != null && <span className="inv-pill__count">{counts[inv.id]}</span>}
        </button>
      ))}
    </div>
  );
}
