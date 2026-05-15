import React, { useMemo, useState } from 'react';

export interface RiskReport {
  ticker: string;
  filing_date: string;
  ai_summary: string[];
  changes: {
    type: 'added' | 'modified';
    text: string;
    context?: string;
  }[];
}

type Props = {
  report: RiskReport;
  className?: string;
  maxInitialRows?: number;
  onOpenFiling?: (ticker: string, filingDate: string) => void;
};

type ScoredChange = RiskReport['changes'][number] & { score: number };

const styles: Record<string, React.CSSProperties> = {
  root: {
    fontFamily: 'ui-sans-serif, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif',
    border: '1px solid #d5dbe4',
    borderRadius: 10,
    background: '#ffffff',
    color: '#0f172a',
    overflow: 'hidden',
  },
  header: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    gap: 12,
    padding: '10px 12px',
    borderBottom: '1px solid #e5eaf1',
    background: '#f8fafc',
  },
  titleWrap: { display: 'flex', alignItems: 'baseline', gap: 10 },
  title: { margin: 0, fontSize: 14, fontWeight: 700, letterSpacing: 0.2 },
  sub: { margin: 0, fontSize: 12, color: '#475569' },
  btn: {
    border: '1px solid #c7d2e0',
    background: '#fff',
    borderRadius: 8,
    fontSize: 12,
    padding: '6px 10px',
    cursor: 'pointer',
  },
  body: { padding: 12, display: 'grid', gap: 12 },
  summaryBox: {
    border: '1px solid #e5eaf1',
    borderRadius: 8,
    background: '#fcfdff',
    padding: '10px 12px',
  },
  summaryTitle: { margin: '0 0 8px 0', fontSize: 12, textTransform: 'uppercase', color: '#475569', letterSpacing: 0.4 },
  bullets: { margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.45 },
  controls: { display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' },
  chip: {
    border: '1px solid #cfd8e6',
    borderRadius: 999,
    padding: '4px 10px',
    fontSize: 11,
    background: '#fff',
    cursor: 'pointer',
  },
  chipOn: { borderColor: '#0f172a', background: '#f1f5f9', fontWeight: 600 },
  search: {
    border: '1px solid #cfd8e6',
    borderRadius: 8,
    fontSize: 12,
    padding: '6px 8px',
    minWidth: 220,
  },
  tableWrap: { border: '1px solid #e5eaf1', borderRadius: 8, overflow: 'hidden' },
  table: { width: '100%', borderCollapse: 'collapse', tableLayout: 'fixed' as const },
  th: {
    textAlign: 'left' as const,
    fontSize: 11,
    letterSpacing: 0.3,
    textTransform: 'uppercase' as const,
    color: '#475569',
    background: '#f8fafc',
    borderBottom: '1px solid #e5eaf1',
    padding: '8px 10px',
  },
  td: {
    verticalAlign: 'top' as const,
    borderBottom: '1px solid #eef2f7',
    padding: '8px 10px',
    fontSize: 12,
    lineHeight: 1.4,
  },
  badgeBase: {
    display: 'inline-block',
    borderRadius: 999,
    fontSize: 10,
    fontWeight: 700,
    letterSpacing: 0.2,
    padding: '2px 8px',
  },
  added: { background: '#e8f7ef', color: '#166534', border: '1px solid #bfe7cf' },
  modified: { background: '#fff7e6', color: '#92400e', border: '1px solid #f2d8a8' },
  rowToggle: {
    border: '1px solid #d4dbe5',
    background: '#fff',
    borderRadius: 6,
    fontSize: 11,
    padding: '3px 6px',
    cursor: 'pointer',
  },
  context: {
    marginTop: 6,
    fontSize: 11,
    color: '#334155',
    background: '#f8fafc',
    border: '1px solid #e2e8f0',
    borderRadius: 6,
    padding: 8,
    whiteSpace: 'pre-wrap' as const,
  },
  topList: {
    border: '1px solid #e5eaf1',
    borderRadius: 8,
    background: '#fffdf7',
    padding: '10px 12px',
  },
  topTitle: { margin: '0 0 8px 0', fontSize: 12, textTransform: 'uppercase', color: '#475569', letterSpacing: 0.4 },
  topRow: {
    display: 'grid',
    gridTemplateColumns: '62px 62px 1fr',
    gap: 8,
    alignItems: 'start',
    padding: '6px 0',
    borderBottom: '1px dashed #e2e8f0',
    fontSize: 12,
  },
  topNum: { color: '#334155', fontWeight: 700 },
  score: {
    display: 'inline-block',
    border: '1px solid #d6c487',
    borderRadius: 999,
    background: '#fff4cc',
    color: '#7a5600',
    padding: '2px 8px',
    fontSize: 10,
    fontWeight: 700,
  },
  footer: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10 },
  meta: { fontSize: 11, color: '#64748b' },
};

const RISK_KEYWORDS = [
  'material weakness',
  'liquidity',
  'default',
  'covenant',
  'investigation',
  'litigation',
  'cybersecurity',
  'impairment',
  'going concern',
  'regulatory',
  'restructuring',
  'revenue decline',
  'customer concentration',
  'supply chain',
  'data breach',
];

function scoreChange(change: RiskReport['changes'][number]): number {
  let score = change.type === 'added' ? 7 : 5;
  const text = `${change.text} ${change.context ?? ''}`.toLowerCase();
  for (const kw of RISK_KEYWORDS) {
    if (text.includes(kw)) score += 2;
  }
  const len = change.text.trim().length;
  if (len > 220) score += 1;
  return score;
}

export default function RiskDiffViewer({
  report,
  className,
  maxInitialRows = 12,
  onOpenFiling,
}: Props) {
  const [filter, setFilter] = useState<'all' | 'added' | 'modified'>('all');
  const [query, setQuery] = useState('');
  const [expandedRows, setExpandedRows] = useState<Record<number, boolean>>({});
  const [showAllRows, setShowAllRows] = useState(false);

  const counts = useMemo(() => {
    const added = report.changes.filter((c) => c.type === 'added').length;
    const modified = report.changes.filter((c) => c.type === 'modified').length;
    return { total: report.changes.length, added, modified };
  }, [report.changes]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return report.changes.filter((c) => {
      if (filter !== 'all' && c.type !== filter) return false;
      if (!q) return true;
      const blob = `${c.text} ${c.context ?? ''}`.toLowerCase();
      return blob.includes(q);
    });
  }, [report.changes, filter, query]);

  const topMaterial = useMemo(() => {
    const scored: ScoredChange[] = report.changes.map((c) => ({ ...c, score: scoreChange(c) }));
    scored.sort((a, b) => b.score - a.score);
    return scored.slice(0, 3);
  }, [report.changes]);

  const visible = useMemo(() => {
    if (showAllRows) return filtered;
    return filtered.slice(0, Math.max(1, maxInitialRows));
  }, [filtered, showAllRows, maxInitialRows]);

  const toggleRow = (idx: number) => {
    setExpandedRows((prev) => ({ ...prev, [idx]: !prev[idx] }));
  };

  const badgeStyle = (type: 'added' | 'modified') => ({
    ...styles.badgeBase,
    ...(type === 'added' ? styles.added : styles.modified),
  });

  return (
    <section className={className} style={styles.root}>
      <header style={styles.header}>
        <div style={styles.titleWrap}>
          <h3 style={styles.title}>Risk Diff - {report.ticker}</h3>
          <p style={styles.sub}>10-K filed {report.filing_date}</p>
        </div>
        {onOpenFiling ? (
          <button style={styles.btn} onClick={() => onOpenFiling(report.ticker, report.filing_date)}>
            Open Filing
          </button>
        ) : null}
      </header>

      <div style={styles.body}>
        <div style={styles.summaryBox}>
          <p style={styles.summaryTitle}>AI Material Risk Increases</p>
          <ul style={styles.bullets}>
            {report.ai_summary.slice(0, 3).map((b, i) => (
              <li key={`${i}-${b.slice(0, 18)}`}>{b}</li>
            ))}
          </ul>
        </div>

        <div style={styles.topList}>
          <p style={styles.topTitle}>Top Material Changes</p>
          {topMaterial.length === 0 ? (
            <div style={styles.meta}>No changes to rank.</div>
          ) : (
            topMaterial.map((row, i) => (
              <div style={styles.topRow} key={`top-${i}-${row.text.slice(0, 16)}`}>
                <div style={styles.topNum}>#{i + 1}</div>
                <div>
                  <span style={styles.score}>Score {row.score}</span>
                </div>
                <div>{row.text}</div>
              </div>
            ))
          )}
        </div>

        <div style={styles.controls}>
          <button style={{ ...styles.chip, ...(filter === 'all' ? styles.chipOn : {}) }} onClick={() => setFilter('all')}>
            All ({counts.total})
          </button>
          <button style={{ ...styles.chip, ...(filter === 'added' ? styles.chipOn : {}) }} onClick={() => setFilter('added')}>
            Added ({counts.added})
          </button>
          <button style={{ ...styles.chip, ...(filter === 'modified' ? styles.chipOn : {}) }} onClick={() => setFilter('modified')}>
            Modified ({counts.modified})
          </button>
          <input
            style={styles.search}
            placeholder="Search risk text or context..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>

        <div style={styles.tableWrap}>
          <table style={styles.table}>
            <thead>
              <tr>
                <th style={{ ...styles.th, width: 100 }}>Type</th>
                <th style={styles.th}>Changed Risk Sentence</th>
                <th style={{ ...styles.th, width: 92 }}>Context</th>
              </tr>
            </thead>
            <tbody>
              {visible.length === 0 ? (
                <tr>
                  <td style={styles.td} colSpan={3}>
                    No matching changes.
                  </td>
                </tr>
              ) : (
                visible.map((row, idx) => {
                  const rowKey = idx;
                  const isOpen = !!expandedRows[rowKey];
                  return (
                    <tr key={`${row.type}-${idx}-${row.text.slice(0, 24)}`}>
                      <td style={styles.td}>
                        <span style={badgeStyle(row.type)}>{row.type.toUpperCase()}</span>
                      </td>
                      <td style={styles.td}>
                        {row.text}
                        {isOpen && row.context ? <div style={styles.context}>{row.context}</div> : null}
                      </td>
                      <td style={styles.td}>
                        {row.context ? (
                          <button style={styles.rowToggle} onClick={() => toggleRow(rowKey)}>
                            {isOpen ? 'Hide' : 'Show'}
                          </button>
                        ) : (
                          <span style={styles.meta}>-</span>
                        )}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        <div style={styles.footer}>
          <div style={styles.meta}>
            Showing {visible.length} of {filtered.length} filtered changes ({counts.total} total).
          </div>
          {filtered.length > maxInitialRows ? (
            <button style={styles.btn} onClick={() => setShowAllRows((v) => !v)}>
              {showAllRows ? 'Show Less' : `Show All (${filtered.length})`}
            </button>
          ) : null}
        </div>
      </div>
    </section>
  );
}
