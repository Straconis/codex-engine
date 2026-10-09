import { useEffect, useId, useState } from "react";
import { formatElapsed, percent } from "./format";

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

/** A run that has just started: nothing reported by the backend yet. */
export function newAIRun(sourceId: number, page: number, title: string): AIRun {
  return { sourceId, page, title, startedAt: Date.now(), done: 0, total: 0, attempt: 0, maxAttempts: 0, written: 0, expected: 0, log: [] };
}

/** 0-1 overall progress: finished parts plus how far the current reply has been written. */
function runProgress(run: AIRun): number {
  if (run.finished?.kind === "ok" || run.finished?.kind === "partial") return 1;
  if (!run.total) return 0;
  const part = run.attempt > 0 && run.expected > 0 ? Math.min(run.written / run.expected, 0.98) : 0;
  return Math.min(1, (run.done + part) / run.total);
}

type Props = {
  run: AIRun;
  readerOnPage: boolean; // the reader is showing this page right now
  onCancel: () => void;
  onClose: () => void;
  onShowPage: () => void;
};

export default function AIProgressWindow({ run, readerOnPage, onCancel, onClose, onShowPage }: Props) {
  const [now, setNow] = useState(() => Date.now());
  const [collapsed, setCollapsed] = useState(false);
  const logId = useId();
  const running = !run.finished;

  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);

  const pct = percent(runProgress(run), 1);
  const indeterminate = running && run.total === 0;
  const part = run.total > 1 ? `Part ${Math.min(run.done + 1, run.total)} of ${run.total}` : "This page";
  const attemptText =
    run.attempt > 1 ? `retry ${run.attempt - 1} of ${run.maxAttempts - 1}, with a correction` : run.attempt === 1 ? "first attempt" : "";
  const recent = run.log.slice(-8);

  return (
    // Only the status line is a live region: the clock and log would be read out on every tick.
    <div className={`aiWindow${run.finished ? ` done-${run.finished.kind}` : ""}`} role="region" aria-label="AI formatting progress">
      <div className="aiWindowHeader">
        <div className="aiWindowTitle">
          <b>{running ? "AI formatting…" : "AI formatting"}</b>
          <span className="hint" title={run.title}>
            {run.title} · p. {run.page}
          </span>
        </div>
        <button
          className="btn small"
          onClick={() => setCollapsed((c) => !c)}
          title={collapsed ? "Show details" : "Hide details"}
          aria-expanded={!collapsed}
          aria-controls={logId}
        >
          {collapsed ? "Details" : "Hide"}
        </button>
      </div>

      <div
        className="barOuter"
        role="progressbar"
        aria-label="AI formatting progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={indeterminate ? undefined : pct}
      >
        <div className={`barInner${indeterminate ? " indeterminate" : ""}`} style={{ width: `${indeterminate ? 30 : pct}%` }} />
      </div>

      <div className="aiWindowStatus">
        <span role="status">
          {run.finished
            ? run.finished.message
            : run.total === 0
            ? "Starting… (loading the model can take a few seconds)"
            : `${part}${attemptText ? ` · ${attemptText}` : ""}${
                run.attempt > 0 && run.expected > 0
                  ? ` · written ${Math.min(run.written, run.expected * 2).toLocaleString()} of ~${run.expected.toLocaleString()} characters`
                  : ""
              }`}
        </span>
        <span className="hint">
          {pct}% · {formatElapsed((run.finished ? run.log[run.log.length - 1]?.at ?? now : now) - run.startedAt)}
        </span>
      </div>

      {!collapsed && recent.length > 0 && (
        <ol className="aiWindowLog" id={logId}>
          {recent.map((entry, i) => (
            <li key={run.log.length - recent.length + i}>
              <span className="hint">{formatElapsed(entry.at - run.startedAt)}</span> {entry.message}
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
