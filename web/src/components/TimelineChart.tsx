import { useEffect, useMemo, useRef, useState } from 'react';
import * as d3 from 'd3';

interface TimelineEvent {
  date: string;
  label: string;
  type: 'financial' | 'legal' | 'communication' | 'corporate' | string;
  evidence_ref?: string | null;
  entity?: string;
  detail?: string;
  importance?: 'high' | 'medium' | 'low';
  highlight?: boolean;
  certainty?: 'documented' | 'inferred' | 'alleged';
}

interface Props {
  events: TimelineEvent[];
  groupBy?: 'entity' | 'type' | 'none';
  height?: number;
}

type DatePrecision = 'day' | 'month' | 'year' | 'range';
type Certainty = 'documented' | 'inferred' | 'alleged';

type ParsedTimelineEvent = TimelineEvent & {
  _id: string;
  _start: Date;
  _end: Date;
  _precision: DatePrecision;
  _certainty: Certainty;
  _score: number;
};

const DAY_MS = 86400000;
const MAX_GROUPS = 7;

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

const EVENT_COLORS: Record<string, string> = {
  financial: C.ember,
  legal: '#b7b1a3',
  communication: C.icy,
  corporate: '#7ea7c1',
};

function eventColor(type: string): string {
  return EVENT_COLORS[type] || C.mithril;
}

function normalizeDateToken(value: string): string {
  return value.trim().replace(/\//g, '-').replace(/\s+/g, '');
}

function parsePointDate(value: string): { start: Date; end: Date; precision: Exclude<DatePrecision, 'range'> } | null {
  const token = normalizeDateToken(value);

  const dayMatch = token.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (dayMatch) {
    const year = Number(dayMatch[1]);
    const month = Number(dayMatch[2]);
    const day = Number(dayMatch[3]);
    const date = new Date(Date.UTC(year, month - 1, day));
    if (!Number.isNaN(date.getTime())) {
      return { start: date, end: date, precision: 'day' };
    }
  }

  const monthMatch = token.match(/^(\d{4})-(\d{2})$/);
  if (monthMatch) {
    const year = Number(monthMatch[1]);
    const month = Number(monthMatch[2]);
    const start = new Date(Date.UTC(year, month - 1, 1));
    const end = new Date(Date.UTC(year, month, 0));
    if (!Number.isNaN(start.getTime()) && !Number.isNaN(end.getTime())) {
      return { start, end, precision: 'month' };
    }
  }

  const yearMatch = token.match(/^(\d{4})$/);
  if (yearMatch) {
    const year = Number(yearMatch[1]);
    const start = new Date(Date.UTC(year, 0, 1));
    const end = new Date(Date.UTC(year, 11, 31));
    return { start, end, precision: 'year' };
  }

  const parsed = new Date(value);
  if (!Number.isNaN(parsed.getTime())) {
    const date = new Date(Date.UTC(parsed.getUTCFullYear(), parsed.getUTCMonth(), parsed.getUTCDate()));
    return { start: date, end: date, precision: 'day' };
  }

  return null;
}

function parseDateWindow(raw: string): { start: Date; end: Date; precision: DatePrecision } | null {
  if (!raw) return null;
  const value = raw.trim();

  const yearRange = value.match(/^(\d{4})\s*[\u2013\u2014\-]\s*(\d{4})$/);
  if (yearRange) {
    const startYear = Number(yearRange[1]);
    const endYear = Number(yearRange[2]);
    const from = new Date(Date.UTC(Math.min(startYear, endYear), 0, 1));
    const to = new Date(Date.UTC(Math.max(startYear, endYear), 11, 31));
    return { start: from, end: to, precision: 'range' };
  }

  const explicitRange = value.match(/^([0-9\-/]{4,10})\s*(?:to|TO|\u2013|\u2014|\.\.)\s*([0-9\-/]{4,10})$/);
  if (explicitRange) {
    const start = parsePointDate(explicitRange[1]);
    const end = parsePointDate(explicitRange[2]);
    if (start && end) {
      const from = start.start.getTime() <= end.start.getTime() ? start.start : end.start;
      const to = start.end.getTime() <= end.end.getTime() ? end.end : start.end;
      return { start: from, end: to, precision: 'range' };
    }
  }

  const point = parsePointDate(value);
  if (!point) return null;

  return {
    start: point.start,
    end: point.end,
    precision: point.precision,
  };
}

function inferCertainty(event: TimelineEvent): Certainty {
  if (event.certainty) return event.certainty;
  if (event.evidence_ref) return 'documented';

  const text = `${event.label} ${event.detail || ''}`.toLowerCase();
  if (text.includes('alleg') || text.includes('possible') || text.includes('unverified') || text.includes('rumor')) {
    return 'alleged';
  }
  return 'inferred';
}

function eventScore(event: TimelineEvent, parsed: { hasRange: boolean; precision: DatePrecision; certainty: Certainty }): number {
  let score = 0;
  if (event.highlight) score += 3;
  if (event.importance === 'high') score += 2;
  if (event.importance === 'medium') score += 1;
  if (event.evidence_ref) score += 1;
  if (event.detail) score += 1;
  if (parsed.hasRange) score += 1;
  if (parsed.precision === 'day') score += 1;
  if (parsed.certainty === 'documented') score += 1;
  return score;
}

function formatDate(date: Date): string {
  return date.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' });
}

function formatDateWindow(event: ParsedTimelineEvent): string {
  if (event._start.getTime() === event._end.getTime()) {
    return formatDate(event._start);
  }

  if (event._precision === 'year') {
    return String(event._start.getUTCFullYear());
  }

  if (event._precision === 'month') {
    return event._start.toLocaleDateString('en-US', { year: 'numeric', month: 'long', timeZone: 'UTC' });
  }

  return `${formatDate(event._start)} to ${formatDate(event._end)}`;
}

function certaintyDash(certainty: Certainty): string {
  if (certainty === 'documented') return '0';
  if (certainty === 'inferred') return '4 3';
  return '2 3';
}

export default function TimelineChart({ events, groupBy = 'none', height = 500 }: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const overviewRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const [width, setWidth] = useState(0);
  const [activeTypes, setActiveTypes] = useState<string[]>([]);
  const [highSignalOnly, setHighSignalOnly] = useState(false);
  const [focusWindow, setFocusWindow] = useState<[Date, Date] | null>(null);
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);

  const parsed = useMemo<ParsedTimelineEvent[]>(() => {
    return events
      .map((event, index) => {
        const window = parseDateWindow(event.date);
        if (!window) return null;

        const certainty = inferCertainty(event);
        const hasRange = window.end.getTime() > window.start.getTime();

        return {
          ...event,
          _id: `${event.date}|${event.label}|${index}`,
          _start: window.start,
          _end: window.end,
          _precision: window.precision,
          _certainty: certainty,
          _score: eventScore(event, { hasRange, precision: window.precision, certainty }),
        };
      })
      .filter((event): event is ParsedTimelineEvent => Boolean(event))
      .sort((a, b) => a._start.getTime() - b._start.getTime() || a.label.localeCompare(b.label));
  }, [events]);

  const typeCounts = useMemo(() => {
    const counts = new Map<string, number>();
    parsed.forEach(event => {
      counts.set(event.type, (counts.get(event.type) || 0) + 1);
    });
    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);
  }, [parsed]);

  useEffect(() => {
    setActiveTypes(prev => {
      const available = typeCounts.map(([type]) => type);
      if (available.length === 0) return [];
      if (prev.length === 0) return available;

      const availableSet = new Set(available);
      const retained = prev.filter(type => availableSet.has(type));
      const missing = available.filter(type => !retained.includes(type));
      return [...retained, ...missing];
    });
  }, [typeCounts]);

  const activeTypeSet = useMemo(() => new Set(activeTypes), [activeTypes]);

  const emphasizedEvents = useMemo(() => {
    if (activeTypeSet.size === 0) return parsed;
    return parsed.filter(event => activeTypeSet.has(event.type));
  }, [parsed, activeTypeSet]);

  const keyEvents = useMemo(() => {
    if (emphasizedEvents.length === 0) return [];
    return [...emphasizedEvents]
      .sort((a, b) => b._score - a._score || a._start.getTime() - b._start.getTime())
      .slice(0, Math.min(8, emphasizedEvents.length))
      .sort((a, b) => a._start.getTime() - b._start.getTime());
  }, [emphasizedEvents]);

  const keyEventMap = useMemo(() => {
    const map = new Map<string, number>();
    keyEvents.forEach((event, index) => map.set(event._id, index + 1));
    return map;
  }, [keyEvents]);

  const displayEvents = useMemo(() => {
    if (!highSignalOnly) return parsed;
    if (parsed.length === 0) return [];

    const keyIds = new Set(keyEvents.map(event => event._id));
    return parsed.filter(event => keyIds.has(event._id));
  }, [parsed, highSignalOnly, keyEvents]);

  const fullDomain = useMemo<[Date, Date] | null>(() => {
    if (parsed.length === 0) return null;
    const min = d3.min(parsed, event => event._start.getTime()) ?? 0;
    const max = d3.max(parsed, event => event._end.getTime()) ?? 0;
    const span = Math.max(max - min, 365 * DAY_MS);
    const pad = Math.max(45 * DAY_MS, Math.round(span * 0.06));
    return [new Date(min - pad), new Date(max + pad)];
  }, [parsed]);

  const domainForView = useMemo<[Date, Date] | null>(() => {
    if (!fullDomain) return null;
    if (!focusWindow) return fullDomain;

    let start = new Date(Math.max(fullDomain[0].getTime(), focusWindow[0].getTime()));
    let end = new Date(Math.min(fullDomain[1].getTime(), focusWindow[1].getTime()));

    if (end.getTime() - start.getTime() < 30 * DAY_MS) {
      const midpoint = (start.getTime() + end.getTime()) / 2;
      start = new Date(midpoint - 15 * DAY_MS);
      end = new Date(midpoint + 15 * DAY_MS);
    }

    return [start, end];
  }, [fullDomain, focusWindow]);

  useEffect(() => {
    if (parsed.length === 0) {
      setSelectedEventId(null);
      return;
    }

    setSelectedEventId(previous => {
      if (previous && parsed.some(event => event._id === previous)) return previous;
      return keyEvents[0]?._id || parsed[0]._id;
    });
  }, [parsed, keyEvents]);

  const selectedEvent = useMemo(() => {
    if (!selectedEventId) return null;
    return parsed.find(event => event._id === selectedEventId) || null;
  }, [parsed, selectedEventId]);

  const denseView = useMemo(() => {
    if (!domainForView || width === 0 || displayEvents.length === 0) return false;

    const marginLeft = groupBy === 'none' ? 28 : 120;
    const innerWidth = Math.max(120, width - marginLeft - 24);
    const inWindowCount = displayEvents.filter(event => (
      event._end.getTime() >= domainForView[0].getTime()
      && event._start.getTime() <= domainForView[1].getTime()
    )).length;

    const spanYears = (domainForView[1].getTime() - domainForView[0].getTime()) / (365 * DAY_MS);
    const eventsPer100Px = inWindowCount / Math.max(1, innerWidth / 100);
    return spanYears > 22 || eventsPer100Px > 5;
  }, [domainForView, width, displayEvents, groupBy]);

  const grouping = useMemo(() => {
    if (groupBy === 'none' || displayEvents.length === 0) {
      return {
        groups: ['All'],
        groupKey: (_event: ParsedTimelineEvent) => 'All',
      };
    }

    const key = groupBy === 'entity' ? 'entity' : 'type';
    const counts = new Map<string, number>();
    displayEvents.forEach(event => {
      const label = (event[key] || 'Unknown') as string;
      counts.set(label, (counts.get(label) || 0) + 1);
    });

    const ranked = Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);
    const top = ranked.slice(0, MAX_GROUPS).map(([label]) => label);
    const hasOther = ranked.length > MAX_GROUPS;

    return {
      groups: hasOther ? [...top, 'Other'] : top,
      groupKey: (event: ParsedTimelineEvent) => {
        const label = (event[key] || 'Unknown') as string;
        return top.includes(label) ? label : 'Other';
      },
    };
  }, [displayEvents, groupBy]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const update = () => {
      const rect = container.getBoundingClientRect();
      const next = Math.floor(rect.width);
      if (next) setWidth(next);
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
    if (!svgRef.current || !width) return;

    const svg = d3.select(svgRef.current);
    svg.selectAll('*').remove();

    if (displayEvents.length === 0 || !domainForView) {
      svg.attr('width', width).attr('height', height).attr('viewBox', `0 0 ${width} ${height}`);
      return;
    }

    const margin = { top: 34, right: 24, bottom: 44, left: groupBy === 'none' ? 28 : 120 };
    const innerWidth = Math.max(120, width - margin.left - margin.right);
    const availableHeight = Math.max(120, height - margin.top - margin.bottom);
    const rowHeight = groupBy === 'none'
      ? availableHeight
      : Math.max(34, Math.floor(availableHeight / Math.max(grouping.groups.length, 1)));
    const innerHeight = groupBy === 'none' ? rowHeight : rowHeight * grouping.groups.length;
    const totalHeight = innerHeight + margin.top + margin.bottom;

    svg
      .attr('width', width)
      .attr('height', totalHeight)
      .attr('viewBox', `0 0 ${width} ${totalHeight}`);

    const xScale = d3.scaleTime().domain(domainForView).range([0, innerWidth]);
    const groupScale = d3.scaleBand<string>()
      .domain(grouping.groups)
      .range([0, innerHeight])
      .padding(0.18);

    const inView = displayEvents.filter(event => event._end.getTime() >= domainForView[0].getTime() && event._start.getTime() <= domainForView[1].getTime());

    const g = svg.append('g').attr('transform', `translate(${margin.left},${margin.top})`);

    const getBaseY = (event: ParsedTimelineEvent): number => {
      if (groupBy === 'none') return innerHeight / 2;
      const row = groupScale(grouping.groupKey(event)) || 0;
      return row + groupScale.bandwidth() / 2;
    };

    const laneBackdrop = g.append('g');

    if (groupBy !== 'none') {
      grouping.groups.forEach(group => {
        const y = (groupScale(group) || 0) + groupScale.bandwidth() / 2;
        laneBackdrop.append('line')
          .attr('x1', 0)
          .attr('x2', innerWidth)
          .attr('y1', y)
          .attr('y2', y)
          .attr('stroke', C.ash)
          .attr('stroke-opacity', 0.45);

        laneBackdrop.append('text')
          .attr('x', -10)
          .attr('y', y)
          .attr('text-anchor', 'end')
          .attr('dominant-baseline', 'middle')
          .attr('fill', C.mithril)
          .attr('font-size', '10px')
          .attr('font-family', '"IBM Plex Mono", monospace')
          .text(group.length > 22 ? `${group.slice(0, 20)}…` : group);
      });
    } else {
      const y = innerHeight / 2;
      laneBackdrop.append('line')
        .attr('x1', 0)
        .attr('x2', innerWidth)
        .attr('y1', y)
        .attr('y2', y)
        .attr('stroke', C.ash)
        .attr('stroke-opacity', 0.45);
    }

    const spanYears = (domainForView[1].getTime() - domainForView[0].getTime()) / (365 * DAY_MS);
    const eventsPer100Px = inView.length / Math.max(1, innerWidth / 100);
    const isDenseView = spanYears > 22 || eventsPer100Px > 5;
    const collisionBucketPx = isDenseView ? 14 : 10;
    const offsetStep = isDenseView ? 4.5 : 6;
    const maxOffset = groupBy === 'none'
      ? Math.max(14, innerHeight / 2 - 8)
      : Math.max(8, groupScale.bandwidth() / 2 - 6);

    const denseYears = d3.rollups(
      inView.filter(event => activeTypeSet.size === 0 || activeTypeSet.has(event.type)),
      values => values.length,
      event => event._start.getUTCFullYear(),
    )
      .filter(([, count]) => count >= 3)
      .sort((a, b) => a[0] - b[0]);

    const denseBands = denseYears.reduce<Array<{ startYear: number; endYear: number; count: number }>>((bands, [year, count]) => {
      const previous = bands[bands.length - 1];
      if (!previous || year > previous.endYear + 1) {
        bands.push({ startYear: year, endYear: year, count });
        return bands;
      }

      previous.endYear = year;
      previous.count += count;
      return bands;
    }, [])
      .sort((a, b) => b.count - a.count)
      .slice(0, 4)
      .sort((a, b) => a.startYear - b.startYear);

    const clusterLayer = g.append('g');
    denseBands.forEach(({ startYear, endYear }) => {
      const start = new Date(Date.UTC(startYear, 0, 1));
      const end = new Date(Date.UTC(endYear, 11, 31));
      const x1 = Math.max(0, xScale(start));
      const x2 = Math.min(innerWidth, xScale(end));
      if (x2 <= 0 || x1 >= innerWidth) return;

      clusterLayer.append('rect')
        .attr('x', x1)
        .attr('y', 0)
        .attr('width', Math.max(1, x2 - x1))
        .attr('height', innerHeight)
        .attr('fill', C.icy)
        .attr('fill-opacity', 0.04);
    });

    const offsetById = new Map<string, number>();
    const perBucketCounts = new Map<string, number>();

    inView.forEach(event => {
      const bucket = Math.round(xScale(event._start) / collisionBucketPx);
      const bucketKey = `${grouping.groupKey(event)}|${bucket}`;
      perBucketCounts.set(bucketKey, (perBucketCounts.get(bucketKey) || 0) + 1);
    });

    const seenPerBucket = new Map<string, number>();
    inView.forEach(event => {
      const bucket = Math.round(xScale(event._start) / collisionBucketPx);
      const bucketKey = `${grouping.groupKey(event)}|${bucket}`;
      const count = perBucketCounts.get(bucketKey) || 1;
      const index = seenPerBucket.get(bucketKey) || 0;
      seenPerBucket.set(bucketKey, index + 1);
      const rawOffset = (index - (count - 1) / 2) * offsetStep;
      const clampedOffset = Math.max(-maxOffset, Math.min(maxOffset, rawOffset));
      offsetById.set(event._id, clampedOffset);
    });

    const ranges = inView.filter(event => event._end.getTime() > event._start.getTime());

    g.append('g')
      .selectAll('line')
      .data(ranges)
      .join('line')
      .attr('x1', event => xScale(event._start))
      .attr('x2', event => xScale(event._end))
      .attr('y1', event => getBaseY(event) + (offsetById.get(event._id) || 0))
      .attr('y2', event => getBaseY(event) + (offsetById.get(event._id) || 0))
      .attr('stroke', event => eventColor(event.type))
      .attr('stroke-opacity', event => (activeTypeSet.size === 0 || activeTypeSet.has(event.type)) ? 0.65 : 0.16)
      .attr('stroke-width', event => (activeTypeSet.size === 0 || activeTypeSet.has(event.type)) ? 2 : 1.4)
      .attr('stroke-dasharray', event => certaintyDash(event._certainty));

    const dots = g.append('g')
      .selectAll('circle')
      .data(inView)
      .join('circle')
      .attr('cx', event => xScale(event._start))
      .attr('cy', event => getBaseY(event) + (offsetById.get(event._id) || 0))
      .attr('r', event => {
        if (keyEventMap.has(event._id)) return isDenseView ? 5.2 : 5.8;
        return isDenseView ? 3.4 : 4.2;
      })
      .attr('fill', event => {
        const active = activeTypeSet.size === 0 || activeTypeSet.has(event.type);
        if (active) return keyEventMap.has(event._id) ? eventColor(event.type) : C.stone;
        return C.stone;
      })
      .attr('fill-opacity', event => {
        const active = activeTypeSet.size === 0 || activeTypeSet.has(event.type);
        if (active) return keyEventMap.has(event._id) ? 0.95 : 0.88;
        return keyEventMap.has(event._id) ? 0.42 : 0.2;
      })
      .attr('stroke', event => eventColor(event.type))
      .attr('stroke-opacity', event => (activeTypeSet.size === 0 || activeTypeSet.has(event.type)) ? 1 : 0.35)
      .attr('stroke-width', event => (keyEventMap.has(event._id) ? 1.6 : 1.2))
      .style('cursor', 'pointer')
      .on('click', (_evt, event) => setSelectedEventId(event._id));

    g.append('g')
      .selectAll('circle')
      .data(inView.filter(event => event._id === selectedEventId))
      .join('circle')
      .attr('cx', event => xScale(event._start))
      .attr('cy', event => getBaseY(event) + (offsetById.get(event._id) || 0))
      .attr('r', isDenseView ? 8 : 9)
      .attr('fill', 'none')
      .attr('stroke', C.icy)
      .attr('stroke-width', 1.4)
      .attr('stroke-opacity', 0.8);

    const keyEventsInView = inView.filter(event => keyEventMap.has(event._id));
    const keyMarkerEvents = (() => {
      if (!isDenseView) return keyEventsInView;

      const kept = new Map<string, ParsedTimelineEvent>();
      const keyBucketSize = collisionBucketPx * 1.4;

      keyEventsInView.forEach(event => {
        const bucket = Math.round(xScale(event._start) / keyBucketSize);
        const bucketKey = `${grouping.groupKey(event)}|${bucket}`;
        const existing = kept.get(bucketKey);
        if (!existing) {
          kept.set(bucketKey, event);
          return;
        }

        const existingRank = keyEventMap.get(existing._id) || Number.POSITIVE_INFINITY;
        const nextRank = keyEventMap.get(event._id) || Number.POSITIVE_INFINITY;
        if (nextRank < existingRank) kept.set(bucketKey, event);
      });

      return Array.from(kept.values())
        .sort((a, b) => a._start.getTime() - b._start.getTime() || a.label.localeCompare(b.label));
    })();

    const keyMarkers = g.append('g')
      .selectAll('g')
      .data(keyMarkerEvents)
      .join('g')
      .attr('transform', event => {
        const y = getBaseY(event) + (offsetById.get(event._id) || 0) - 13;
        return `translate(${xScale(event._start)},${y})`;
      });

    keyMarkers.append('circle')
      .attr('r', 8)
      .attr('fill', C.slate)
      .attr('stroke', C.ash)
      .attr('stroke-width', 1);

    keyMarkers.append('text')
      .attr('text-anchor', 'middle')
      .attr('dominant-baseline', 'central')
      .attr('font-family', '"IBM Plex Mono", monospace')
      .attr('font-size', '9px')
      .attr('fill', C.moonlight)
      .text(event => String(keyEventMap.get(event._id) || ''));

    const desiredTicks = Math.max(4, Math.floor(innerWidth / (spanYears > 40 ? 120 : 95)));
    const tickFormat = spanYears > 24
      ? d3.timeFormat('%Y')
      : spanYears > 6
        ? d3.timeFormat("%b '%y")
        : d3.timeFormat('%b %Y');

    const axis = g.append('g')
      .attr('transform', `translate(0,${innerHeight})`)
      .call(d3.axisBottom(xScale).ticks(desiredTicks).tickFormat(tickFormat as any).tickSizeOuter(0));

    axis.selectAll('text').attr('fill', C.mithril).attr('font-size', '10px');
    axis.selectAll('line').attr('stroke', C.ash);
    axis.selectAll('.domain').attr('stroke', C.ash);

    const tickTexts = axis.selectAll<SVGTextElement, Date>('text');
    const hideAlternateTicks = tickTexts.size() > Math.floor(innerWidth / 70);
    if (hideAlternateTicks) {
      tickTexts.attr('display', (_value, index) => (index % 2 === 0 ? null : 'none'));
    }

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
      .style('font-family', '"Space Grotesk", sans-serif')
      .style('pointer-events', 'none')
      .style('z-index', '1000')
      .style('max-width', '340px');

    dots
      .on('mouseover', (_event, event) => {
        tooltip.style('visibility', 'visible').html([
          `<strong>${event.label}</strong>`,
          `<span style=\"color:${eventColor(event.type)}\">${event.type}</span> · ${formatDateWindow(event)}`,
          event.entity ? `Entity: ${event.entity}` : '',
          event.detail || '',
          `Certainty: ${event._certainty}`,
          event.evidence_ref ? `<span style=\"color:${C.mithril}\">Ref: ${event.evidence_ref}</span>` : '',
        ].filter(Boolean).join('<br/>'));
      })
      .on('mousemove', event => {
        tooltip
          .style('top', `${event.pageY - 10}px`)
          .style('left', `${event.pageX + 10}px`);
      })
      .on('mouseout', () => tooltip.style('visibility', 'hidden'));

    return () => {
      tooltip.remove();
    };
  }, [displayEvents, domainForView, width, height, groupBy, grouping, keyEventMap, selectedEventId, activeTypeSet]);

  useEffect(() => {
    if (!overviewRef.current || !width) return;

    const svg = d3.select(overviewRef.current);
    svg.selectAll('*').remove();

    const overviewHeight = 76;
    svg
      .attr('width', width)
      .attr('height', overviewHeight)
      .attr('viewBox', `0 0 ${width} ${overviewHeight}`);

    if (!fullDomain || parsed.length === 0) return;

    const margin = { top: 8, right: 14, bottom: 16, left: 14 };
    const innerWidth = Math.max(120, width - margin.left - margin.right);
    const innerHeight = Math.max(12, overviewHeight - margin.top - margin.bottom);

    const x = d3.scaleTime().domain(fullDomain).range([0, innerWidth]);

    const yearlyDensity = d3.rollups(
      parsed,
      values => values.length,
      event => d3.timeYear.floor(event._start).getTime(),
    ).sort((a, b) => a[0] - b[0]);

    const y = d3.scaleLinear()
      .domain([0, d3.max(yearlyDensity, item => item[1]) || 1])
      .range([innerHeight, 0]);

    const g = svg.append('g').attr('transform', `translate(${margin.left},${margin.top})`);

    g.append('rect')
      .attr('x', 0)
      .attr('y', 0)
      .attr('width', innerWidth)
      .attr('height', innerHeight)
      .attr('fill', 'rgba(11,13,16,0.55)')
      .attr('stroke', C.ash)
      .attr('stroke-opacity', 0.6);

    g.selectAll('rect.density')
      .data(yearlyDensity)
      .join('rect')
      .attr('class', 'density')
      .attr('x', item => x(new Date(item[0])))
      .attr('y', item => y(item[1]))
      .attr('width', item => {
        const nextYear = new Date(item[0]);
        nextYear.setUTCFullYear(nextYear.getUTCFullYear() + 1);
        return Math.max(1, x(nextYear) - x(new Date(item[0])) - 1);
      })
      .attr('height', item => innerHeight - y(item[1]))
      .attr('fill', C.icy)
      .attr('fill-opacity', 0.26);

    const brush = d3.brushX()
      .extent([[0, 0], [innerWidth, innerHeight]])
      .on('end', event => {
        if (!event.selection) {
          setFocusWindow(null);
          return;
        }

        const [x0, x1] = event.selection as [number, number];
        const start = x.invert(x0);
        const end = x.invert(x1);

        if (end.getTime() - start.getTime() < 30 * DAY_MS) return;

        const hasChanged = !focusWindow
          || Math.abs(focusWindow[0].getTime() - start.getTime()) > DAY_MS
          || Math.abs(focusWindow[1].getTime() - end.getTime()) > DAY_MS;

        if (hasChanged) {
          setFocusWindow([start, end]);
        }
      });

    const brushLayer = g.append('g').call(brush as any);
    brushLayer.selectAll('.selection')
      .attr('fill', C.icy)
      .attr('fill-opacity', 0.16)
      .attr('stroke', C.icy)
      .attr('stroke-opacity', 0.8);
    brushLayer.selectAll('.handle')
      .attr('fill', C.icy)
      .attr('fill-opacity', 0.9);

    if (focusWindow) {
      const clampedStart = new Date(Math.max(fullDomain[0].getTime(), focusWindow[0].getTime()));
      const clampedEnd = new Date(Math.min(fullDomain[1].getTime(), focusWindow[1].getTime()));
      brushLayer.call(brush.move as any, [x(clampedStart), x(clampedEnd)]);
    }
  }, [width, fullDomain, parsed, focusWindow]);

  const toggleType = (type: string) => {
    setActiveTypes(previous => {
      if (previous.includes(type)) {
        const next = previous.filter(item => item !== type);
        return next.length ? next : previous;
      }
      return [...previous, type];
    });
  };

  const focusOnEvent = (event: ParsedTimelineEvent) => {
    setSelectedEventId(event._id);
    if (!fullDomain) return;

    const span = Math.max(event._end.getTime() - event._start.getTime(), 180 * DAY_MS);
    let start = new Date(event._start.getTime() - span);
    let end = new Date(event._end.getTime() + span);

    if (start.getTime() < fullDomain[0].getTime()) start = fullDomain[0];
    if (end.getTime() > fullDomain[1].getTime()) end = fullDomain[1];

    setFocusWindow([start, end]);
  };

  return (
    <div ref={containerRef} className="surface p-4">
      <div className="flex items-center justify-between gap-3 mb-3">
        <div className="text-xs text-mithril font-mono">
          {displayEvents.length} visible · {emphasizedEvents.length} emphasized · {groupBy === 'none' ? 'single lane' : `grouped by ${groupBy}`}{denseView ? ' · compressed density mode' : ''}
        </div>
        <button
          type="button"
          onClick={() => setFocusWindow(null)}
          className="text-xs text-mithril hover:text-moon disabled:opacity-40"
          disabled={!focusWindow}
        >
          Reset time window
        </button>
      </div>

      <div className="flex flex-wrap gap-2 mb-3">
        {typeCounts.map(([type, count]) => {
          const active = activeTypes.includes(type);
          return (
            <button
              key={type}
              type="button"
              onClick={() => toggleType(type)}
              className={`inline-flex items-center gap-2 rounded border px-2.5 py-1 text-xs transition ${active
                ? 'border-[color:var(--color-icy)] text-moon bg-[rgba(18,21,27,0.9)]'
                : 'border-[color:var(--color-ash)] text-mithril bg-[rgba(18,21,27,0.55)]'}`}
            >
              <span className="inline-block w-2.5 h-2.5 rounded-full" style={{ background: eventColor(type) }} />
              {type}
              <span className="text-[10px] text-mithril">({count})</span>
            </button>
          );
        })}

        <label className="inline-flex items-center gap-2 rounded border border-[color:var(--color-ash)] bg-[rgba(18,21,27,0.55)] px-2.5 py-1 text-xs text-mithril">
          <input
            type="checkbox"
            checked={highSignalOnly}
            onChange={event => setHighSignalOnly(event.target.checked)}
          />
          High-signal only
        </label>
      </div>

      <div className="surface-muted p-2 mb-3">
        <div className="text-[11px] text-mithril font-mono mb-1">Overview (drag to set timeline window)</div>
        <svg ref={overviewRef} className="w-full" />
      </div>

      <svg ref={svgRef} className="w-full graph-canvas" style={{ height: `${height}px` }} />

      <div className="mt-4 grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(240px,320px)]">
        <div className="space-y-2 text-xs text-mithril" style={{ fontFamily: 'var(--font-body)' }}>
          <div className="text-moon" style={{ fontSize: '0.65rem', letterSpacing: '0.2em', textTransform: 'uppercase', fontFamily: 'var(--font-ui)', fontWeight: 500 }}>
            Key Events
          </div>
          {keyEvents.length === 0 ? (
            <div className="text-mithril">No events for the current filters.</div>
          ) : (
            keyEvents.map((event, index) => (
              <button
                key={event._id}
                type="button"
                onClick={() => focusOnEvent(event)}
                className={`w-full text-left rounded border px-2 py-2 transition ${selectedEventId === event._id
                  ? 'border-[color:var(--color-icy)] bg-[rgba(18,21,27,0.85)]'
                  : 'border-[color:var(--color-ash)] bg-[rgba(18,21,27,0.55)]'}`}
              >
                <div className="flex items-start gap-2">
                  <span
                    className="inline-flex items-center justify-center"
                    style={{
                      width: '18px',
                      height: '18px',
                      borderRadius: '999px',
                      border: `1px solid ${C.ash}`,
                      color: C.moonlight,
                      fontFamily: 'var(--font-mono)',
                      fontSize: '0.6rem',
                      flex: '0 0 auto',
                      lineHeight: 1,
                    }}
                  >
                    {index + 1}
                  </span>
                  <span>
                    <span className="text-moon">{event.label}</span>
                    <span className="text-mithril"> · {formatDateWindow(event)}</span>
                    {event.evidence_ref ? <span className="block text-[11px] text-mithril mt-0.5">Ref: {event.evidence_ref}</span> : null}
                  </span>
                </div>
              </button>
            ))
          )}
        </div>

        <div className="surface-muted p-3">
          <div className="text-moon mb-2" style={{ fontSize: '0.65rem', letterSpacing: '0.2em', textTransform: 'uppercase', fontFamily: 'var(--font-ui)', fontWeight: 500 }}>
            Selected Event
          </div>
          {selectedEvent ? (
            <div className="space-y-2 text-xs text-mithril" style={{ fontFamily: 'var(--font-body)' }}>
              <div className="text-sm text-moon">{selectedEvent.label}</div>
              <div>
                <span style={{ color: eventColor(selectedEvent.type) }}>{selectedEvent.type}</span>
                <span className="text-mithril"> · {formatDateWindow(selectedEvent)}</span>
              </div>
              {selectedEvent.entity ? <div>Entity: {selectedEvent.entity}</div> : null}
              {selectedEvent.detail ? <div>{selectedEvent.detail}</div> : null}
              <div>Certainty: {selectedEvent._certainty}</div>
              {selectedEvent.evidence_ref ? <div>Evidence: {selectedEvent.evidence_ref}</div> : null}
            </div>
          ) : (
            <div className="text-xs text-mithril">Select an event to inspect context and evidence scaffolding.</div>
          )}
        </div>
      </div>

      <div className="mt-2 text-xs text-mithril text-center font-mono">
        Default view communicates key events; interactions add context and detail.
      </div>
    </div>
  );
}
