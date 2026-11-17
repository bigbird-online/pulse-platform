export function StatusMessage({ error, success }: { error: string | null; success: string | null }) {
  return (
    <>
      {error ? <div className="msg error" role="alert">{error}</div> : null}
      {success ? <div className="msg ok" role="status">{success}</div> : null}
    </>
  );
}
