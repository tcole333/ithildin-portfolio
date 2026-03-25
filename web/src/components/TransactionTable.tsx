import { useState, useMemo } from 'react';

interface Transaction {
  date: string;
  from: string;
  to: string;
  amount: number;
  type?: string;
  evidence_ref?: string;
  description?: string;
}

interface Props {
  transactions: Transaction[];
  title?: string;
}

type SortKey = 'date' | 'from' | 'to' | 'amount' | 'type';
type SortDir = 'asc' | 'desc';

function formatAmount(value: number): string {
  if (value >= 1_000_000_000) return `$${(value / 1_000_000_000).toFixed(2)}B`;
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `$${(value / 1_000).toFixed(1)}K`;
  return `$${value.toLocaleString()}`;
}

function formatDate(dateStr: string): string {
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return dateStr;
  return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
}

const COLORS = {
  text: '#c7d0d9',
  muted: '#8c97a3',
  icy: '#8fd3e8',
  ember: '#d1b36a',
  ash: '#2a313b',
  stone: '#12151b',
};

export default function TransactionTable({ transactions, title }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>('date');
  const [sortDir, setSortDir] = useState<SortDir>('desc');
  const [entityFilter, setEntityFilter] = useState('');
  const [minAmount, setMinAmount] = useState('');
  const [maxAmount, setMaxAmount] = useState('');

  const entities = useMemo(() => {
    const set = new Set<string>();
    transactions.forEach(t => { set.add(t.from); set.add(t.to); });
    return Array.from(set).sort();
  }, [transactions]);

  const filtered = useMemo(() => {
    let rows = [...transactions];
    const term = entityFilter.toLowerCase().trim();
    if (term) {
      rows = rows.filter(t =>
        t.from.toLowerCase().includes(term) || t.to.toLowerCase().includes(term)
      );
    }
    const min = minAmount ? parseFloat(minAmount) : null;
    const max = maxAmount ? parseFloat(maxAmount) : null;
    if (min !== null) rows = rows.filter(t => t.amount >= min);
    if (max !== null) rows = rows.filter(t => t.amount <= max);
    return rows;
  }, [transactions, entityFilter, minAmount, maxAmount]);

  const sorted = useMemo(() => {
    const rows = [...filtered];
    rows.sort((a, b) => {
      let cmp = 0;
      switch (sortKey) {
        case 'date': cmp = new Date(a.date).getTime() - new Date(b.date).getTime(); break;
        case 'from': cmp = a.from.localeCompare(b.from); break;
        case 'to': cmp = a.to.localeCompare(b.to); break;
        case 'amount': cmp = a.amount - b.amount; break;
        case 'type': cmp = (a.type || '').localeCompare(b.type || ''); break;
      }
      return sortDir === 'asc' ? cmp : -cmp;
    });
    return rows;
  }, [filtered, sortKey, sortDir]);

  const runningTotal = useMemo(() => {
    return sorted.reduce((sum, t) => sum + t.amount, 0);
  }, [sorted]);

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    } else {
      setSortKey(key);
      setSortDir(key === 'amount' || key === 'date' ? 'desc' : 'asc');
    }
  };

  const sortIndicator = (key: SortKey) => {
    if (sortKey !== key) return '';
    return sortDir === 'asc' ? ' \u25B4' : ' \u25BE';
  };

  const headerClass = 'px-3 py-2 text-left text-xs uppercase tracking-wider cursor-pointer select-none transition-colors hover:text-moon';

  return (
    <div className="surface p-4">
      {title && <h3 className="text-lg font-semibold text-moon mb-4">{title}</h3>}

      <div className="flex flex-wrap gap-3 mb-4">
        <input
          value={entityFilter}
          onChange={e => setEntityFilter(e.target.value)}
          placeholder="Filter by entity..."
          className="rounded border border-[color:var(--color-ash)] bg-[rgba(11,13,16,0.6)] px-3 py-1.5 text-sm text-moon placeholder:text-mithril w-48"
        />
        <div className="flex items-center gap-2 text-xs text-mithril">
          <span>Amount:</span>
          <input
            value={minAmount}
            onChange={e => setMinAmount(e.target.value)}
            placeholder="Min"
            type="number"
            className="rounded border border-[color:var(--color-ash)] bg-[rgba(11,13,16,0.6)] px-2 py-1.5 text-sm text-moon w-24 font-mono"
          />
          <span>&ndash;</span>
          <input
            value={maxAmount}
            onChange={e => setMaxAmount(e.target.value)}
            placeholder="Max"
            type="number"
            className="rounded border border-[color:var(--color-ash)] bg-[rgba(11,13,16,0.6)] px-2 py-1.5 text-sm text-moon w-24 font-mono"
          />
        </div>
        <div className="flex items-center gap-3 ml-auto text-xs text-mithril font-mono">
          <span>{sorted.length} rows</span>
          <span className="text-ember">{formatAmount(runningTotal)} total</span>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-[color:var(--color-ash)]">
              <th className={`${headerClass} ${sortKey === 'date' ? 'text-moon' : 'text-mithril'}`} onClick={() => handleSort('date')}>
                Date{sortIndicator('date')}
              </th>
              <th className={`${headerClass} ${sortKey === 'from' ? 'text-moon' : 'text-mithril'}`} onClick={() => handleSort('from')}>
                From{sortIndicator('from')}
              </th>
              <th className={`${headerClass} ${sortKey === 'to' ? 'text-moon' : 'text-mithril'}`} onClick={() => handleSort('to')}>
                To{sortIndicator('to')}
              </th>
              <th className={`${headerClass} text-right ${sortKey === 'amount' ? 'text-moon' : 'text-mithril'}`} onClick={() => handleSort('amount')}>
                Amount{sortIndicator('amount')}
              </th>
              <th className={`${headerClass} ${sortKey === 'type' ? 'text-moon' : 'text-mithril'}`} onClick={() => handleSort('type')}>
                Type{sortIndicator('type')}
              </th>
              <th className="px-3 py-2 text-left text-xs uppercase tracking-wider text-mithril">
                Ref
              </th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((t, i) => (
              <tr
                key={i}
                className="border-b border-[rgba(42,49,59,0.4)] transition-colors hover:bg-[rgba(143,211,232,0.04)]"
              >
                <td className="px-3 py-2 font-mono text-xs text-mithril whitespace-nowrap">
                  {formatDate(t.date)}
                </td>
                <td className="px-3 py-2 text-moon">{t.from}</td>
                <td className="px-3 py-2 text-moon">{t.to}</td>
                <td className="px-3 py-2 text-right font-mono text-ember whitespace-nowrap">
                  {formatAmount(t.amount)}
                </td>
                <td className="px-3 py-2 text-xs text-mithril">{t.type || ''}</td>
                <td className="px-3 py-2 text-xs">
                  {t.evidence_ref ? (
                    <span className="text-icy font-mono">{t.evidence_ref}</span>
                  ) : (
                    <span className="text-mithril">&mdash;</span>
                  )}
                </td>
              </tr>
            ))}
            {sorted.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-8 text-center text-mithril">
                  No transactions match the current filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
