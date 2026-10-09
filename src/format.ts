// Small text helpers shared by the progress windows and status lines.

/** Elapsed time as m:ss. */
export function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** current/total as a whole percentage, 0-100 (0 while the total is unknown). */
export function percent(current: number, total: number): number {
  return total > 0 ? Math.min(100, Math.max(0, Math.round((current / total) * 100))) : 0;
}

/** "1 page", "3 pages"; pass `many` for irregular plurals ("1 match", "2 matches"). */
export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}
