const WRAP_PATTERNS = [
  /^\s*\/wrap\s*[。！!]?\s*$/i,
  /^\s*执行\s*\/wrap\s*[。！!]?\s*$/i,
];

export function hasWrapEvidence(text: string): boolean {
  return WRAP_PATTERNS.some((pattern) => pattern.test(text));
}

// A blocked closeout must never leave its request marker eligible for /new.
// These are explicit /wrap protocol blocker codes, not natural-language success claims.
export function hasWrapBlockerCode(text: string): boolean {
  return /\b(?:BLOCKED_WRAP_[A-Z0-9_]+|BLOCKED_TRANSCRIPT_MISSING|BLOCKED_COMPUTE_PROFILE_RESOLUTION)\b/.test(text);
}
