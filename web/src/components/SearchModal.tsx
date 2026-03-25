import { useState, useEffect, useRef, useCallback } from 'react';
import type MiniSearch from 'minisearch';
import { getSearchEngine, searchWithRanking, type SearchDocument, type RankedResult } from '../lib/searchEngine';

const TYPE_LABELS: Record<string, string> = {
  dossier: 'Dossier',
  article: 'Article',
  model: 'Model',
};

const TYPE_COLORS: Record<string, string> = {
  dossier: 'var(--color-icy)',
  article: 'var(--color-ember)',
  model: 'var(--color-mithril)',
};

export default function SearchModal() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<RankedResult[]>([]);
  const [activeIndex, setActiveIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const engineRef = useRef<MiniSearch<SearchDocument> | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const doOpen = useCallback(async () => {
    setOpen(true);
    document.body.style.overflow = 'hidden';
    if (!engineRef.current) {
      setLoading(true);
      engineRef.current = await getSearchEngine();
      setLoading(false);
    }
    setTimeout(() => inputRef.current?.focus(), 50);
  }, []);

  const doClose = useCallback(() => {
    setOpen(false);
    setQuery('');
    setResults([]);
    setActiveIndex(0);
    document.body.style.overflow = '';
  }, []);

  // Cmd+K / Ctrl+K global listener
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        if (open) doClose();
        else doOpen();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open, doOpen, doClose]);

  // CustomEvent from nav button
  useEffect(() => {
    const handler = () => doOpen();
    window.addEventListener('open-search', handler);
    return () => window.removeEventListener('open-search', handler);
  }, [doOpen]);

  // Search on query change
  useEffect(() => {
    if (!engineRef.current || !query.trim()) {
      setResults([]);
      setActiveIndex(0);
      return;
    }
    const ranked = searchWithRanking(engineRef.current, query);
    setResults(ranked);
    setActiveIndex(0);
  }, [query]);

  // Scroll active result into view
  useEffect(() => {
    if (!listRef.current) return;
    const active = listRef.current.children[activeIndex] as HTMLElement | undefined;
    active?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex]);

  const navigate = (href: string) => {
    doClose();
    window.location.href = href;
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIndex(i => Math.min(i + 1, results.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIndex(i => Math.max(i - 1, 0));
    } else if (e.key === 'Enter' && results[activeIndex]) {
      e.preventDefault();
      navigate(results[activeIndex].href);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      doClose();
    }
  };

  if (!open) return null;

  return (
    <div className="search-overlay" onClick={doClose}>
      <div className="search-modal" onClick={e => e.stopPropagation()} onKeyDown={onKeyDown}>
        <div className="search-input-wrap">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--color-mithril)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="11" cy="11" r="8" />
            <line x1="21" y1="21" x2="16.65" y2="16.65" />
          </svg>
          <input
            ref={inputRef}
            type="text"
            className="search-input"
            placeholder="Search dossiers, articles, models..."
            value={query}
            onChange={e => setQuery(e.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
          <kbd className="search-kbd">ESC</kbd>
        </div>

        <div className="search-results" ref={listRef}>
          {loading && (
            <div className="search-empty">Loading index...</div>
          )}

          {!loading && query && results.length === 0 && (
            <div className="search-empty">No results for "{query}"</div>
          )}

          {!loading && !query && (
            <div className="search-empty">Type to search across all content</div>
          )}

          {results.map((r, i) => (
            <a
              key={r.id}
              href={r.href}
              className={`search-result ${i === activeIndex ? 'search-result--active' : ''}`}
              onMouseEnter={() => setActiveIndex(i)}
              onClick={e => { e.preventDefault(); navigate(r.href); }}
            >
              <span className="search-result__badge" style={{ borderColor: TYPE_COLORS[r.type], color: TYPE_COLORS[r.type] }}>
                {TYPE_LABELS[r.type]}
              </span>
              <div className="search-result__body">
                <div className="search-result__title">{r.title}</div>
                {r.description && (
                  <div className="search-result__desc">
                    {r.description.length > 120 ? r.description.slice(0, 120) + '...' : r.description}
                  </div>
                )}
              </div>
              {r.tier === 'cross-reference' && r.mentionCount > 0 && (
                <span className="search-result__mentions">{r.mentionCount} conn.</span>
              )}
              {r.stats && r.tier === 'primary' && (
                <span className="search-result__stats">{r.stats}</span>
              )}
            </a>
          ))}
        </div>
      </div>
    </div>
  );
}
