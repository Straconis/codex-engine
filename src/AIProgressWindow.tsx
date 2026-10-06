import { useEffect, useState } from "react";

// Floating window that shows what AI formatting is doing: a progress bar fed by the model's
// streamed reply, the current part and attempt, and a plain-language log. It sits above the
// reader but doesn't block it, so you can keep reading while a page is formatted.

export type AIRun = {
  sourceId: number;
  page: number;
  title: string;
  startedAt: number;
  done: number; // parts finished
  total: number; // parts on the page (0 until the backend reports)
  attempt: number; // attempt running on the current part (0 = page finished)
  maxAttempts: number;
  written: number; // characters of the current reply streamed so far
  expected: number; // characters a faithful reply needs (the part's length)
  log: { at: number; message: string }[];
  finished?: { kind: "ok" | "partial" | "decision" | "cancelled" | "error"; message: string };
};

/** 0-1 overall progress: finished parts plus how far the current reply has been written. */
export function runProgress(run: AIRun): number {
  if (run.finished) return run.finished.kind === "ok" || run.finished.kind === "partial" ? 1 : progressSoFar(run);
  return progressSoFar(run);
}

function progressSoFar(run: AIRun): number {
  if (!run.total) return 0;
  const part = run.attempt > 0 && run.expected > 0 ? Math.min(run.written / run.expected, 0.98) : 0;
  return Math.min(1, (run.done + part) / run.total);
}

function elapsed(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

type Props = {
  run: AIRun;
  readerOnPage: boolean; // the reader is showing this page right now
  onCancel: () => void;
  onClose: () => void;
  onShowPage: () => void;
};

export default function AIProgressWindow({ run, readerOnPage, onCancel, onClose, onShowPage }: Props) {
  const [now, setNow] = useState(Date.now());
  const [collapsed, setCollapsed] = useState(false);
  const running = !run.finished;

  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);

  const pct = Math.round(runProgress(run) * 100);
  const part = run.total > 1 ? `Part ${Math.min(run.done + 1, run.total)} of ${run.total}` : "This page";
  const attemptText =
    run.attempt > 1 ? `retry ${run.attempt - 1} of ${run.maxAttempts - 1}, with a correction` : run.attempt === 1 ? "first attempt" : "";
  const recent = run.log.slice(-8);

  return (
    <div className={`aiWindow${run.finished ? ` done-${run.finished.kind}` : ""}`} role="status" aria-live="polite">
      <div className="aiWindowHeader">
        <div className="aiWindowTitle">
          <b>{running ? "AI formatting…" : "AI formatting"}</b>
          <span className="hint" title={run.title}>
            {run.title} • p. {run.page}
          </span>
        </div>
        <button className="btn small" onClick={() => setCollapsed((c) => !c)} title={collapsed ? "Show details" : "Hide details"}>
          {collapsed ? "Details" : "Hide"}
        </button>
      </div>

      <div className="barOuter" aria-label={`${pct}% done`}>
        <div className={`barInner${running && run.total === 0 ? " indeterminate" : ""}`} style={{ width: `${running && run.total === 0 ? 30 : pct}%` }} />
      </div>

      <div className="aiWindowStatus">
        {running ? (
          run.total === 0 ? (
            <span>Starting… (loading the model can take a few seconds)</span>
          ) : (
            <span>
              {part}
              {attemptText ? ` • ${attemptText}` : ""}
              {run.attempt > 0 && run.expected > 0
                ? ` • written ${Math.min(run.written, run.expected * 2).toLocaleString()} of ~${run.expected.toLocaleString()} characters`
                : ""}
            </span>
          )
        ) : (
          <span>{run.finished!.message}</span>
        )}
        <span className="hint">
          {pct}% • {elapsed((run.finished ? run.log[run.log.length - 1]?.at ?? now : now) - run.startedAt)}
        </span>
      </div>

      {!collapsed && recent.length > 0 && (
        <ol className="aiWindowLog">
          {recent.map((entry, i) => (
            <li key={run.log.length - recent.length + i}>
              <span className="hint">{elapsed(entry.at - run.startedAt)}</span> {entry.message}
            </li>
          ))}
        </ol>
      )}

      <div className="aiWindowActions">
        {running ? (
          <>
            <span className="hint">Runs on this computer. You can keep reading.</span>
            <button className="btn small danger" onClick={onCancel}>
              Cancel
            </button>
          </>
        ) : (
          <>
            {!readerOnPage && (
              <button className="btn small" onClick={onShowPage}>
                Show page
              </button>
            )}
            <button className="btn small" onClick={onClose}>
              Close
            </button>
          </>
        )}
      </div>
    </div>
  );
}
