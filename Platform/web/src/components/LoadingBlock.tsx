export function LoadingBlock({ text }: { text?: string }) {
  return (
    <div className="loading-block">
      <div className="spinner" />
      {text ? <p style={{ marginTop: '12px' }}>{text}</p> : null}
    </div>
  );
}
