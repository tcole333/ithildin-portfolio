import { useEffect, useMemo, useState } from 'react';

interface FlowLink {
  source: string;
  target: string;
  value: number;
  tx_count?: number;
  first_date?: string;
  last_date?: string;
}

interface BalancePoint {
  date: string;
  balance: number;
}

interface TopTransaction {
  tx_date: string;
  amount: number;
  sender?: string | null;
  receiver?: string | null;
  reference?: string | null;
  efta_id?: string | null;
}

interface DS10Data {
  links: FlowLink[];
  top_transactions?: TopTransaction[];
  balances?: Record<string, BalancePoint[]>;
  stats?: {
    total_nodes?: number;
    total_links?: number;
    total_value?: number;
  };
}

interface Props {
  data: DS10Data;
  dossierSlugByEntityKey?: Record<string, string>;
}

type AggregatedLink = {
  id: string;
  sourceKey: string;
  targetKey: string;
  sourceLabel: string;
  targetLabel: string;
  value: number;
  txCount: number;
  firstDate?: string;
  lastDate?: string;
};

type EntitySummary = {
  key: string;
  label: string;
  inbound: number;
  outbound: number;
  txCount: number;
  counterparties: number;
};

type NormalizedTransaction = {
  id: string;
  date: string;
  amount: number;
  senderKey: string;
  senderLabel: string;
  receiverKey: string;
  receiverLabel: string;
  reference: string;
  eftaId?: string;
};

type BalanceSeries = {
  key: string;
  label: string;
  points: BalancePoint[];
  peak: number;
  latest: number;
};

type ProcessedDS10 = {
  rawEntityCount: number;
  canonicalEntityCount: number;
  aliasMerges: number;
  aggregatedLinks: AggregatedLink[];
  entities: EntitySummary[];
  normalizedTransactions: NormalizedTransaction[];
  balanceSeries: BalanceSeries[];
  totalValue: number;
  topCorridor: AggregatedLink | null;
  topHub: EntitySummary | null;
  topTransaction: NormalizedTransaction | null;
  rangeStart?: string;
  rangeEnd?: string;
};

const MIN_AMOUNT_OPTIONS = [50_000, 250_000, 1_000_000, 5_000_000];

const C = {
  void: '#0b0d10',
  stone: '#12151b',
  ash: '#2a313b',
  mithril: '#8c97a3',
  moonlight: '#c7d0d9',
  icy: '#8fd3e8',
  ember: '#d1b36a',
};

function cleanLabel(raw?: string | null): string {
  const value = `${raw || ''}`.replace(/\s+/g, ' ').trim();
  return value || 'Unknown';
}

function canonicalEntity(raw?: string | null): string {
  const cleaned = cleanLabel(raw);
  return cleaned
    .toUpperCase()
    .replace(/[^A-Z0-9]+/g, ' ')
    .replace(/\bN A\b/g, 'NA')
    .replace(/\bA C\b/g, 'AC')
    .replace(/\bI N C\b/g, 'INC')
    .replace(/\bL L C\b/g, 'LLC')
    .replace(/\s+/g, ' ')
    .trim();
}

function chooseDisplayName(candidates: Set<string>): string {
  const values = Array.from(candidates).map(cleanLabel).filter(Boolean);
  if (values.length === 0) return 'Unknown';

  const mixedCase = values.filter(value => /[a-z]/.test(value));
  const pool = mixedCase.length > 0 ? mixedCase : values;

  return [...pool].sort((a, b) => a.length - b.length || a.localeCompare(b))[0];
}

function parseDateToken(token?: string): number | null {
  if (!token) return null;
  const stamp = Date.parse(token);
  return Number.isNaN(stamp) ? null : stamp;
}

function minDateToken(a?: string, b?: string): string | undefined {
  if (!a) return b;
  if (!b) return a;
  const aStamp = parseDateToken(a);
  const bStamp = parseDateToken(b);
  if (aStamp === null) return b;
  if (bStamp === null) return a;
  return aStamp <= bStamp ? a : b;
}

function maxDateToken(a?: string, b?: string): string | undefined {
  if (!a) return b;
  if (!b) return a;
  const aStamp = parseDateToken(a);
  const bStamp = parseDateToken(b);
  if (aStamp === null) return b;
  if (bStamp === null) return a;
  return aStamp >= bStamp ? a : b;
}

function formatDateToken(token?: string): string {
  if (!token) return 'Unknown';
  const stamp = parseDateToken(token);
  if (stamp === null) return token;
  return new Date(stamp).toLocaleDateString('en-US', { year: 'numeric', month: 'short', timeZone: 'UTC' });
}

function formatMoney(value: number): string {
  if (value >= 1_000_000_000) return `$${(value / 1_000_000_000).toFixed(1)}B`;
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `$${(value / 1_000).toFixed(0)}K`;
  return `$${value.toLocaleString()}`;
}

function formatThreshold(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M+`;
  return `${(value / 1_000).toFixed(0)}K+`;
}

function buildSparklinePath(points: BalancePoint[], width = 180, height = 56): string {
  if (points.length === 0) return '';
  const pad = 4;
  const parsed = points
    .map(point => ({ ...point, stamp: parseDateToken(point.date) }))
    .filter((point): point is BalancePoint & { stamp: number } => point.stamp !== null);

  if (parsed.length === 0) return '';

  const minTime = Math.min(...parsed.map(point => point.stamp));
  const maxTime = Math.max(...parsed.map(point => point.stamp));
  const maxBalance = Math.max(...parsed.map(point => Math.max(point.balance, 0)), 1);
  const timeSpan = Math.max(maxTime - minTime, 1);

  return parsed
    .map((point, index) => {
      const x = pad + ((point.stamp - minTime) / timeSpan) * (width - pad * 2);
      const y = height - pad - (Math.max(point.balance, 0) / maxBalance) * (height - pad * 2);
      return `${index === 0 ? 'M' : 'L'}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(' ');
}

function isEftaId(value?: string): boolean {
  return Boolean(value && /^EFTA\d{6,}$/i.test(value));
}

function summarizeReference(reference: string): string {
  if (!reference) return '-';
  if (reference.length <= 80) return reference;
  return `${reference.slice(0, 78)}...`;
}

function dossierHref(entityKey: string, map?: Record<string, string>): string | null {
  if (!map) return null;
  const slug = map[entityKey];
  if (!slug) return null;
  return `/dossiers/${slug}`;
}

function processDS10(data: DS10Data): ProcessedDS10 {
  const links = Array.isArray(data.links) ? data.links : [];
  const topTransactions = Array.isArray(data.top_transactions) ? data.top_transactions : [];
  const balances = data.balances || {};

  const aliasesByKey = new Map<string, Set<string>>();
  const touchAlias = (raw?: string | null): string => {
    const key = canonicalEntity(raw);
    if (!aliasesByKey.has(key)) aliasesByKey.set(key, new Set<string>());
    aliasesByKey.get(key)!.add(cleanLabel(raw));
    return key;
  };

  links.forEach(link => {
    touchAlias(link.source);
    touchAlias(link.target);
  });

  topTransactions.forEach(transaction => {
    touchAlias(transaction.sender);
    touchAlias(transaction.receiver);
  });

  Object.keys(balances).forEach(name => {
    touchAlias(name);
  });

  const displayByKey = new Map<string, string>();
  aliasesByKey.forEach((candidates, key) => {
    displayByKey.set(key, chooseDisplayName(candidates));
  });

  const aggregatedMap = new Map<string, AggregatedLink>();
  links.forEach(link => {
    const value = Number(link.value);
    if (!Number.isFinite(value) || value <= 0) return;

    const sourceKey = touchAlias(link.source);
    const targetKey = touchAlias(link.target);
    if (sourceKey === targetKey) return;

    const id = `${sourceKey}->${targetKey}`;
    const existing = aggregatedMap.get(id);
    if (!existing) {
      aggregatedMap.set(id, {
        id,
        sourceKey,
        targetKey,
        sourceLabel: displayByKey.get(sourceKey) || cleanLabel(link.source),
        targetLabel: displayByKey.get(targetKey) || cleanLabel(link.target),
        value,
        txCount: link.tx_count && link.tx_count > 0 ? link.tx_count : 1,
        firstDate: link.first_date,
        lastDate: link.last_date,
      });
      return;
    }

    existing.value += value;
    existing.txCount += link.tx_count && link.tx_count > 0 ? link.tx_count : 1;
    existing.firstDate = minDateToken(existing.firstDate, link.first_date);
    existing.lastDate = maxDateToken(existing.lastDate, link.last_date);
  });

  const aggregatedLinks = Array.from(aggregatedMap.values())
    .sort((a, b) => b.value - a.value || a.sourceLabel.localeCompare(b.sourceLabel));

  type EntityAccumulator = {
    key: string;
    label: string;
    inbound: number;
    outbound: number;
    txCount: number;
    counterparties: Set<string>;
  };

  const entityAcc = new Map<string, EntityAccumulator>();
  const ensureEntity = (key: string): EntityAccumulator => {
    const existing = entityAcc.get(key);
    if (existing) return existing;
    const next: EntityAccumulator = {
      key,
      label: displayByKey.get(key) || key,
      inbound: 0,
      outbound: 0,
      txCount: 0,
      counterparties: new Set<string>(),
    };
    entityAcc.set(key, next);
    return next;
  };

  aggregatedLinks.forEach(link => {
    const source = ensureEntity(link.sourceKey);
    const target = ensureEntity(link.targetKey);

    source.outbound += link.value;
    target.inbound += link.value;
    source.txCount += link.txCount;
    target.txCount += link.txCount;
    source.counterparties.add(target.key);
    target.counterparties.add(source.key);
  });

  const entities: EntitySummary[] = Array.from(entityAcc.values())
    .map(entity => ({
      key: entity.key,
      label: entity.label,
      inbound: entity.inbound,
      outbound: entity.outbound,
      txCount: entity.txCount,
      counterparties: entity.counterparties.size,
    }))
    .sort((a, b) => (b.inbound + b.outbound) - (a.inbound + a.outbound) || a.label.localeCompare(b.label));

  const normalizedTransactions: NormalizedTransaction[] = topTransactions.map((transaction, index) => {
    const senderKey = touchAlias(transaction.sender);
    const receiverKey = touchAlias(transaction.receiver);
    const senderLabel = displayByKey.get(senderKey) || cleanLabel(transaction.sender);
    const receiverLabel = displayByKey.get(receiverKey) || cleanLabel(transaction.receiver);
    const efta = transaction.efta_id ? transaction.efta_id.toUpperCase() : undefined;

    const reference = `${transaction.reference || ''}`.replace(/\s+/g, ' ').trim();

    return {
      id: `${transaction.tx_date}|${transaction.amount}|${index}`,
      date: transaction.tx_date,
      amount: Number(transaction.amount) || 0,
      senderKey,
      senderLabel,
      receiverKey,
      receiverLabel,
      reference,
      eftaId: efta,
    };
  });

  const topTransaction = [...normalizedTransactions]
    .sort((a, b) => b.amount - a.amount || b.date.localeCompare(a.date))[0] || null;

  const balanceSeries: BalanceSeries[] = Object.entries(balances)
    .map(([name, points]) => {
      const key = touchAlias(name);
      const label = displayByKey.get(key) || cleanLabel(name);

      const sorted = [...(Array.isArray(points) ? points : [])]
        .filter(point => Number.isFinite(point.balance) && parseDateToken(point.date) !== null)
        .sort((a, b) => (parseDateToken(a.date) || 0) - (parseDateToken(b.date) || 0));

      if (sorted.length === 0) return null;

      return {
        key,
        label,
        points: sorted,
        peak: Math.max(...sorted.map(point => point.balance)),
        latest: sorted[sorted.length - 1].balance,
      };
    })
    .filter((series): series is BalanceSeries => Boolean(series))
    .sort((a, b) => b.peak - a.peak || a.label.localeCompare(b.label));

  const dates = aggregatedLinks.flatMap(link => [link.firstDate, link.lastDate]).filter((date): date is string => Boolean(date));
  const rangeStart = dates.reduce<string | undefined>((min, date) => minDateToken(min, date), undefined);
  const rangeEnd = dates.reduce<string | undefined>((max, date) => maxDateToken(max, date), undefined);

  const rawEntityCount = Array.from(aliasesByKey.values()).reduce((sum, set) => sum + set.size, 0);
  const canonicalEntityCount = aliasesByKey.size;
  const aliasMerges = Math.max(0, rawEntityCount - canonicalEntityCount);

  return {
    rawEntityCount,
    canonicalEntityCount,
    aliasMerges,
    aggregatedLinks,
    entities,
    normalizedTransactions,
    balanceSeries,
    totalValue: aggregatedLinks.reduce((sum, link) => sum + link.value, 0),
    topCorridor: aggregatedLinks[0] || null,
    topHub: entities[0] || null,
    topTransaction,
    rangeStart,
    rangeEnd,
  };
}

export default function FinancialWorkbench({ data, dossierSlugByEntityKey }: Props) {
  const processed = useMemo(() => processDS10(data), [data]);

  const [focusEntity, setFocusEntity] = useState<string>('ALL');
  const [minAmount, setMinAmount] = useState<number>(250_000);
  const [rowsLimit, setRowsLimit] = useState<number>(20);
  const [isHydrated, setIsHydrated] = useState<boolean>(false);

  useEffect(() => {
    setIsHydrated(true);
  }, []);

  useEffect(() => {
    if (focusEntity === 'ALL') return;
    if (processed.entities.some(entity => entity.key === focusEntity)) return;
    setFocusEntity('ALL');
  }, [focusEntity, processed.entities]);

  useEffect(() => {
    setRowsLimit(20);
  }, [focusEntity, minAmount]);

  const filteredLinks = useMemo(() => {
    return processed.aggregatedLinks.filter(link => {
      if (link.value < minAmount) return false;
      if (focusEntity === 'ALL') return true;
      return link.sourceKey === focusEntity || link.targetKey === focusEntity;
    });
  }, [processed.aggregatedLinks, minAmount, focusEntity]);

  const maxFilteredValue = useMemo(() => {
    return filteredLinks.length > 0 ? filteredLinks[0].value : 1;
  }, [filteredLinks]);

  const focusedEntity = useMemo(() => {
    if (focusEntity === 'ALL') return null;
    return processed.entities.find(entity => entity.key === focusEntity) || null;
  }, [processed.entities, focusEntity]);

  const focusedEntityHref = useMemo(() => {
    if (!focusedEntity) return null;
    return dossierHref(focusedEntity.key, dossierSlugByEntityKey);
  }, [focusedEntity, dossierSlugByEntityKey]);

  const inboundForFocus = useMemo(() => {
    if (focusEntity === 'ALL') return [];
    return processed.aggregatedLinks
      .filter(link => link.targetKey === focusEntity && link.value >= minAmount)
      .sort((a, b) => b.value - a.value)
      .slice(0, 8);
  }, [processed.aggregatedLinks, focusEntity, minAmount]);

  const outboundForFocus = useMemo(() => {
    if (focusEntity === 'ALL') return [];
    return processed.aggregatedLinks
      .filter(link => link.sourceKey === focusEntity && link.value >= minAmount)
      .sort((a, b) => b.value - a.value)
      .slice(0, 8);
  }, [processed.aggregatedLinks, focusEntity, minAmount]);

  const filteredTransactions = useMemo(() => {
    return processed.normalizedTransactions
      .filter(transaction => {
        if (transaction.amount < minAmount) return false;
        if (focusEntity === 'ALL') return true;
        return transaction.senderKey === focusEntity || transaction.receiverKey === focusEntity;
      })
      .sort((a, b) => b.amount - a.amount || b.date.localeCompare(a.date));
  }, [processed.normalizedTransactions, minAmount, focusEntity]);

  const visibleBalanceSeries = useMemo(() => {
    if (focusEntity === 'ALL') {
      return processed.balanceSeries.slice(0, 4);
    }
    const focused = processed.balanceSeries.filter(series => series.key === focusEntity);
    if (focused.length > 0) return focused;
    return processed.balanceSeries.slice(0, 2);
  }, [processed.balanceSeries, focusEntity]);

  const topFiveConcentration = useMemo(() => {
    if (processed.totalValue <= 0) return 0;
    const topFive = processed.aggregatedLinks.slice(0, 5).reduce((sum, link) => sum + link.value, 0);
    return Math.round((topFive / processed.totalValue) * 100);
  }, [processed.aggregatedLinks, processed.totalValue]);

  const renderEntityName = (entityKey: string, label: string, className: string) => {
    const href = dossierHref(entityKey, dossierSlugByEntityKey);
    if (!href) return <span className={className}>{label}</span>;
    return (
      <a href={href} className={`${className} hover:underline underline-offset-2`}>
        {label}
      </a>
    );
  };

  return (
    <div
      className="surface p-4"
      data-testid="financial-workbench"
      data-focus-entity={focusEntity}
      data-hydrated={isHydrated ? 'true' : 'false'}
    >
      <div className="space-y-2 mb-4">
        <div className="section-label">DS10 Data</div>
        <h2 className="text-xl font-semibold text-moon">Deutsche Bank Flows</h2>
        <p className="text-sm text-mithril">
          Use filters to review entities, links, and source transactions.
        </p>
      </div>

      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5 mb-4">
        <div className="surface-muted p-2.5">
          <div className="text-[10px] text-mithril font-mono uppercase tracking-[0.2em]">Traced value</div>
          <div className="text-sm text-ember font-mono mt-1">{formatMoney(processed.totalValue)}</div>
        </div>
        <div className="surface-muted p-2.5">
          <div className="text-[10px] text-mithril font-mono uppercase tracking-[0.2em]">Links</div>
          <div className="text-sm text-moon font-mono mt-1">{processed.aggregatedLinks.length}</div>
        </div>
        <div className="surface-muted p-2.5">
          <div className="text-[10px] text-mithril font-mono uppercase tracking-[0.2em]">Canonical entities</div>
          <div className="text-sm text-moon font-mono mt-1">{processed.canonicalEntityCount}</div>
        </div>
        <div className="surface-muted p-2.5">
          <div className="text-[10px] text-mithril font-mono uppercase tracking-[0.2em]">Alias merges</div>
          <div className="text-sm text-moon font-mono mt-1">{processed.aliasMerges}</div>
        </div>
        <div className="surface-muted p-2.5">
          <div className="text-[10px] text-mithril font-mono uppercase tracking-[0.2em]">Date span</div>
          <div className="text-sm text-moon font-mono mt-1">
            {formatDateToken(processed.rangeStart)} - {formatDateToken(processed.rangeEnd)}
          </div>
        </div>
      </div>

      <div className="surface-muted p-3 mb-4">
        <div className="text-[10px] text-mithril font-mono uppercase tracking-[0.2em]">Key Signals</div>
        <div className="grid gap-2 lg:grid-cols-3 mt-2">
          <div className="rounded border border-[color:var(--color-ash)] bg-[rgba(11,13,16,0.45)] p-2">
            <div className="text-[10px] text-mithril font-mono uppercase tracking-[0.2em]">Largest link</div>
            <div className="text-xs text-moon mt-1">
              {processed.topCorridor
                ? `${processed.topCorridor.sourceLabel} -> ${processed.topCorridor.targetLabel}`
                : 'No link data'}
            </div>
            <div className="text-[11px] text-ember font-mono mt-1">
              {processed.topCorridor ? formatMoney(processed.topCorridor.value) : '-'}
            </div>
          </div>

          <div className="rounded border border-[color:var(--color-ash)] bg-[rgba(11,13,16,0.45)] p-2">
            <div className="text-[10px] text-mithril font-mono uppercase tracking-[0.2em]">Dominant hub</div>
            <div className="text-xs text-moon mt-1">{processed.topHub?.label || 'No entity data'}</div>
            <div className="text-[11px] text-mithril font-mono mt-1">
              {processed.topHub
                ? `${formatMoney(processed.topHub.inbound + processed.topHub.outbound)} total flow`
                : '-'}
            </div>
          </div>

          <div className="rounded border border-[color:var(--color-ash)] bg-[rgba(11,13,16,0.45)] p-2">
            <div className="text-[10px] text-mithril font-mono uppercase tracking-[0.2em]">Largest single transfer</div>
            <div className="text-xs text-moon mt-1">
              {processed.topTransaction
                ? `${processed.topTransaction.senderLabel} -> ${processed.topTransaction.receiverLabel}`
                : 'No transaction data'}
            </div>
            <div className="text-[11px] text-ember font-mono mt-1">
              {processed.topTransaction ? `${formatMoney(processed.topTransaction.amount)} | ${formatDateToken(processed.topTransaction.date)}` : '-'}
            </div>
          </div>
        </div>

        <div className="text-[11px] text-mithril font-mono mt-2">
          Top 5 links account for {topFiveConcentration}% of traced value. Normalization merged {processed.aliasMerges} noisy labels.
        </div>
      </div>

        <div className="flex flex-wrap items-center gap-2 mb-4">
        <label htmlFor="financial-workbench-focus" className="text-xs text-mithril font-mono">Focus entity</label>
        <select
          id="financial-workbench-focus"
          value={focusEntity}
          onChange={event => setFocusEntity(event.target.value)}
          disabled={!isHydrated}
          className="rounded border border-[color:var(--color-ash)] bg-[rgba(11,13,16,0.62)] px-2 py-1 text-xs text-moon"
        >
          <option value="ALL">All entities</option>
          {processed.entities.map(entity => (
            <option key={entity.key} value={entity.key}>
              {entity.label}
            </option>
          ))}
        </select>

        <span className="text-xs text-mithril font-mono ml-2">Min flow</span>
        <div className="flex flex-wrap gap-1">
          {MIN_AMOUNT_OPTIONS.map(option => {
            const active = option === minAmount;
            return (
              <button
                key={option}
                type="button"
                onClick={() => setMinAmount(option)}
                disabled={!isHydrated}
                className={`rounded border px-2 py-1 text-xs font-mono transition ${active
                  ? 'border-[color:var(--color-icy)] text-moon bg-[rgba(18,21,27,0.88)]'
                  : 'border-[color:var(--color-ash)] text-mithril bg-[rgba(18,21,27,0.5)]'}`}
              >
                {formatThreshold(option)}
              </button>
            );
          })}
        </div>
      </div>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_330px] mb-4">
        <div className="surface-muted p-3">
          <div className="text-[10px] text-mithril font-mono uppercase tracking-[0.2em] mb-2" data-testid="primary-corridors-count">
            Primary Links ({filteredLinks.length})
          </div>

          {filteredLinks.length === 0 ? (
            <div className="text-xs text-mithril">No links match this threshold and focus combination.</div>
          ) : (
            <div className="space-y-2">
              {filteredLinks.slice(0, 16).map(link => {
                const widthPct = Math.max(5, Math.round((link.value / maxFilteredValue) * 100));
                const sourceHref = dossierHref(link.sourceKey, dossierSlugByEntityKey);
                const targetHref = dossierHref(link.targetKey, dossierSlugByEntityKey);
                return (
                  <div key={link.id} className="rounded border border-[color:var(--color-ash)] bg-[rgba(11,13,16,0.42)] p-2">
                    <div className="flex items-start justify-between gap-2">
                      <div className="text-xs">
                        {renderEntityName(link.sourceKey, link.sourceLabel, 'text-moon')}
                        <span className="text-mithril"> {'->'} </span>
                        {renderEntityName(link.targetKey, link.targetLabel, 'text-moon')}
                      </div>
                      <div className="text-[11px] text-ember font-mono">{formatMoney(link.value)}</div>
                    </div>
                    {(sourceHref || targetHref) && (
                      <div className="mt-1 flex flex-wrap gap-2 text-[10px] font-mono">
                        {sourceHref && (
                          <a href={sourceHref} className="text-[color:var(--color-icy)] hover:underline underline-offset-2">
                            dossier {link.sourceLabel}
                          </a>
                        )}
                        {targetHref && (
                          <a href={targetHref} className="text-[color:var(--color-icy)] hover:underline underline-offset-2">
                            dossier {link.targetLabel}
                          </a>
                        )}
                      </div>
                    )}
                    <div className="mt-1 h-1.5 rounded bg-[rgba(42,49,59,0.5)]">
                      <div
                        className="h-full rounded bg-[color:var(--color-icy)]"
                        style={{ width: `${widthPct}%`, opacity: 0.82 }}
                      />
                    </div>
                    <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-mithril font-mono">
                      <span>{link.txCount} tx</span>
                      <span>{formatDateToken(link.firstDate)} {'->'} {formatDateToken(link.lastDate)}</span>
                    </div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      <button
                        type="button"
                        onClick={() => setFocusEntity(link.sourceKey)}
                        disabled={!isHydrated}
                        className="rounded border border-[color:var(--color-ash)] px-1.5 py-0.5 text-[10px] text-mithril hover:text-moon"
                      >
                        focus {link.sourceLabel}
                      </button>
                      <button
                        type="button"
                        onClick={() => setFocusEntity(link.targetKey)}
                        disabled={!isHydrated}
                        className="rounded border border-[color:var(--color-ash)] px-1.5 py-0.5 text-[10px] text-mithril hover:text-moon"
                      >
                        focus {link.targetLabel}
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="surface-muted p-3">
          <div className="text-[10px] text-mithril font-mono uppercase tracking-[0.2em] mb-2">
            {focusEntity === 'ALL' ? 'Entity Heatmap' : 'Entity Lens'}
          </div>

          {focusEntity === 'ALL' ? (
            <div className="space-y-2">
              {processed.entities.slice(0, 8).map(entity => {
                const total = entity.inbound + entity.outbound;
                const entityHref = dossierHref(entity.key, dossierSlugByEntityKey);
                return (
                  <div
                    key={entity.key}
                    className="w-full rounded border border-[color:var(--color-ash)] bg-[rgba(11,13,16,0.42)] p-2 text-left"
                  >
                    <button
                      type="button"
                      onClick={() => setFocusEntity(entity.key)}
                      disabled={!isHydrated}
                      data-testid="heatmap-focus-button"
                      className="w-full text-left"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="text-xs text-moon">{entity.label}</div>
                        <div className="rounded border border-[color:var(--color-ash)] px-1.5 py-0.5 text-[10px] text-mithril">
                          focus
                        </div>
                      </div>
                      <div className="mt-1 text-[10px] text-mithril font-mono">
                        {formatMoney(total)} total | in {formatMoney(entity.inbound)} | out {formatMoney(entity.outbound)}
                      </div>
                      <div className="text-[10px] text-mithril font-mono mt-0.5">
                        {entity.counterparties} counterparties | {entity.txCount} tx
                      </div>
                    </button>
                    {entityHref && (
                      <div>
                        <a
                          href={entityHref}
                          className="inline-block text-[10px] text-[color:var(--color-icy)] font-mono hover:underline underline-offset-2"
                        >
                          open dossier
                        </a>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="space-y-3">
              <div className="rounded border border-[color:var(--color-ash)] bg-[rgba(11,13,16,0.42)] p-2">
                <div className="text-sm">
                  {focusedEntity
                    ? renderEntityName(focusedEntity.key, focusedEntity.label, 'text-moon')
                    : <span className="text-moon">Unknown entity</span>}
                </div>
                <div className="text-[11px] text-mithril font-mono mt-1">
                  In {formatMoney(focusedEntity?.inbound || 0)} | Out {formatMoney(focusedEntity?.outbound || 0)}
                </div>
                <div className="text-[11px] text-mithril font-mono">
                  Counterparties {focusedEntity?.counterparties || 0}
                </div>
                {focusedEntityHref && (
                  <a
                    href={focusedEntityHref}
                    className="mt-1 inline-block text-[10px] text-[color:var(--color-icy)] font-mono hover:underline underline-offset-2"
                  >
                    open dossier
                  </a>
                )}
              </div>

              <div>
                <div className="text-[10px] text-mithril font-mono uppercase tracking-[0.2em] mb-1">Inbound</div>
                <div className="space-y-1.5">
                  {inboundForFocus.length === 0 ? (
                    <div className="text-xs text-mithril">No inbound links at this threshold.</div>
                  ) : inboundForFocus.map(link => (
                    <div
                      key={link.id}
                      className="w-full rounded border border-[color:var(--color-ash)] bg-[rgba(11,13,16,0.42)] px-2 py-1.5 text-left"
                    >
                      <button
                        type="button"
                        onClick={() => setFocusEntity(link.sourceKey)}
                        disabled={!isHydrated}
                        className="w-full text-left"
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div>
                            <div className="text-xs text-moon">{link.sourceLabel}</div>
                            <div className="text-[10px] text-ember font-mono mt-0.5">{formatMoney(link.value)}</div>
                          </div>
                          <div className="rounded border border-[color:var(--color-ash)] px-1.5 py-0.5 text-[10px] text-mithril">
                            focus
                          </div>
                        </div>
                      </button>
                    </div>
                  ))}
                </div>
              </div>

              <div>
                <div className="text-[10px] text-mithril font-mono uppercase tracking-[0.2em] mb-1">Outbound</div>
                <div className="space-y-1.5">
                  {outboundForFocus.length === 0 ? (
                    <div className="text-xs text-mithril">No outbound links at this threshold.</div>
                  ) : outboundForFocus.map(link => (
                    <div
                      key={link.id}
                      className="w-full rounded border border-[color:var(--color-ash)] bg-[rgba(11,13,16,0.42)] px-2 py-1.5 text-left"
                    >
                      <button
                        type="button"
                        onClick={() => setFocusEntity(link.targetKey)}
                        disabled={!isHydrated}
                        className="w-full text-left"
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div>
                            <div className="text-xs text-moon">{link.targetLabel}</div>
                            <div className="text-[10px] text-ember font-mono mt-0.5">{formatMoney(link.value)}</div>
                          </div>
                          <div className="rounded border border-[color:var(--color-ash)] px-1.5 py-0.5 text-[10px] text-mithril">
                            focus
                          </div>
                        </div>
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      {visibleBalanceSeries.length > 0 && (
        <div className="surface-muted p-3 mb-4">
          <div className="text-[10px] text-mithril font-mono uppercase tracking-[0.2em] mb-2">Balance Trajectories</div>
          <div className="grid gap-2 md:grid-cols-2">
            {visibleBalanceSeries.map(series => (
              <div key={series.key} className="rounded border border-[color:var(--color-ash)] bg-[rgba(11,13,16,0.42)] p-2">
                <div className="text-xs">{renderEntityName(series.key, series.label, 'text-moon')}</div>
                <svg width="100%" height="56" viewBox="0 0 180 56" className="mt-1">
                  <path d={buildSparklinePath(series.points)} fill="none" stroke={C.icy} strokeWidth="1.6" />
                  <line x1="0" x2="180" y1="55" y2="55" stroke={C.ash} strokeOpacity="0.7" />
                </svg>
                <div className="flex items-center justify-between gap-2 text-[10px] text-mithril font-mono">
                  <span>peak {formatMoney(series.peak)}</span>
                  <span>latest {formatMoney(series.latest)}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="surface-muted p-3">
        <div className="flex items-center justify-between gap-2 mb-2">
          <div className="text-[10px] text-mithril font-mono uppercase tracking-[0.2em]">Evidence Ledger</div>
          <div className="text-[10px] text-mithril font-mono">
            {Math.min(rowsLimit, filteredTransactions.length)} / {filteredTransactions.length} rows
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-[color:var(--color-ash)] text-[10px] uppercase tracking-[0.2em] text-mithril">
                <th className="py-1.5 pr-3 text-left font-mono">Date</th>
                <th className="py-1.5 pr-3 text-left font-mono">Amount</th>
                <th className="py-1.5 pr-3 text-left font-mono">From</th>
                <th className="py-1.5 pr-3 text-left font-mono">To</th>
                <th className="py-1.5 pr-3 text-left font-mono">Reference</th>
                <th className="py-1.5 text-left font-mono">Evidence</th>
              </tr>
            </thead>
            <tbody>
              {filteredTransactions.slice(0, rowsLimit).map(transaction => (
                <tr key={transaction.id} className="border-b border-[rgba(42,49,59,0.35)] align-top">
                  <td className="py-1.5 pr-3 text-mithril font-mono whitespace-nowrap">{formatDateToken(transaction.date)}</td>
                  <td className="py-1.5 pr-3 text-ember font-mono whitespace-nowrap">{formatMoney(transaction.amount)}</td>
                  <td className="py-1.5 pr-3">
                    {renderEntityName(transaction.senderKey, transaction.senderLabel, 'text-moon')}
                  </td>
                  <td className="py-1.5 pr-3">
                    {renderEntityName(transaction.receiverKey, transaction.receiverLabel, 'text-moon')}
                  </td>
                  <td className="py-1.5 pr-3 text-mithril" title={transaction.reference}>
                    {summarizeReference(transaction.reference)}
                  </td>
                  <td className="py-1.5 text-mithril font-mono whitespace-nowrap">
                    {isEftaId(transaction.eftaId) ? (
                      <a
                        href={`https://jmail.world/thread/${transaction.eftaId}`}
                        target="_blank"
                        rel="noreferrer"
                        className="text-[color:var(--color-icy)] hover:underline"
                      >
                        {transaction.eftaId}
                      </a>
                    ) : (
                      <span>-</span>
                    )}
                  </td>
                </tr>
              ))}
              {filteredTransactions.length === 0 && (
                <tr>
                  <td colSpan={6} className="py-4 text-center text-mithril">
                    No transactions match the current focus and threshold.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {filteredTransactions.length > rowsLimit && (
          <div className="mt-2">
            <button
              type="button"
              onClick={() => setRowsLimit(previous => previous + 20)}
              className="rounded border border-[color:var(--color-ash)] px-2 py-1 text-[11px] text-mithril hover:text-moon"
            >
              Show 20 more
            </button>
          </div>
        )}

        <div className="mt-2 text-[11px] text-mithril font-mono">
          Ledger rows come from the DS10 high-value transaction export and keep EFTA-level evidence traceable.
        </div>
      </div>
    </div>
  );
}
