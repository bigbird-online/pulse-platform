type Props = {
  total: number;
  pageSize: number;
  current: number;
  onChange: (page: number) => void;
};

export function Paginator({ total, pageSize, current, onChange }: Props) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (pages <= 1) return null;

  const buildRange = (): (number | '...')[] => {
    if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1);
    const items: (number | '...')[] = [1];
    const left = Math.max(2, current - 1);
    const right = Math.min(pages - 1, current + 1);
    if (left > 2) items.push('...');
    for (let i = left; i <= right; i++) items.push(i);
    if (right < pages - 1) items.push('...');
    items.push(pages);
    return items;
  };

  return (
    <nav className="paginator" aria-label="分页">
      <button
        type="button"
        className="paginator-btn"
        disabled={current <= 1}
        onClick={() => onChange(current - 1)}
      >
        ‹
      </button>
      {buildRange().map((p, i) =>
        p === '...' ? (
          <span key={`ellipsis-${i}`} className="paginator-ellipsis">…</span>
        ) : (
          <button
            key={p}
            type="button"
            className={`paginator-btn${p === current ? ' paginator-active' : ''}`}
            onClick={() => onChange(p)}
          >
            {p}
          </button>
        ),
      )}
      <button
        type="button"
        className="paginator-btn"
        disabled={current >= pages}
        onClick={() => onChange(current + 1)}
      >
        ›
      </button>
    </nav>
  );
}
