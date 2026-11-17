type Page = 'landing' | 'signup' | 'billing' | 'admin-billing';

export function NavBar({ page, onNavigate }: { page: Page; onNavigate: () => void }) {
  return (
    <nav className="nav">
      <div className="nav-inner">
        <div>
          <span className="nav-brand">BigBird</span>
          <span className="nav-tag"> · Platform</span>
        </div>
        <div className="nav-links">
          {page === 'signup' && (
            <button className="btn-secondary btn-sm" onClick={onNavigate}>
              产品介绍
            </button>
          )}
          {page === 'billing' && (
            <button className="btn-secondary btn-sm" onClick={onNavigate}>
              返回首页
            </button>
          )}
          {page === 'admin-billing' && (
            <button className="btn-secondary btn-sm" onClick={onNavigate}>
              返回首页
            </button>
          )}
        </div>
      </div>
    </nav>
  );
}
