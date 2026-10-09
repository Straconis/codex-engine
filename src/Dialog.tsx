import { useEffect, useId, useRef, type ReactNode } from "react";

// A modal pop-up: a dimmed overlay with a dialog box. While open it holds the keyboard focus
// (moved into it on open, given back to whatever had it on close), and Escape closes the
// top-most dialog only.

// Open dialogs, oldest first: only the last one answers Escape.
const openDialogs: symbol[] = [];

function isTextField(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable || target.tagName === "TEXTAREA" || target.tagName === "SELECT") return true;
  return target instanceof HTMLInputElement && !["checkbox", "radio", "button", "submit", "reset"].includes(target.type);
}

type Props = {
  /** The dialog's header; the element given `titleId` as its id names the dialog. */
  header: (titleId: string) => ReactNode;
  /** Escape (and a click on the dim background, if `closeOnBackdrop`). Omit to keep the dialog open. */
  onClose?: () => void;
  closeOnBackdrop?: boolean;
  /** Extra class for the dialog box (e.g. "reader", "settings"). */
  className?: string;
  /** The reader sits below the AI progress window; every other pop-up sits above it. */
  layer?: "reader" | "popup";
  children: ReactNode;
};

export default function Dialog({ header, onClose, closeOnBackdrop = false, className = "", layer = "popup", children }: Props) {
  const titleId = useId();
  const boxRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    const me = Symbol("dialog");
    openDialogs.push(me);
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    // Something inside may have taken focus already (autoFocus); otherwise focus the dialog itself.
    if (boxRef.current && !boxRef.current.contains(document.activeElement)) boxRef.current.focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented || openDialogs[openDialogs.length - 1] !== me) return;
      if (isTextField(e.target) || !onCloseRef.current) return;
      e.preventDefault();
      onCloseRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      openDialogs.splice(openDialogs.indexOf(me), 1);
      if (previous && previous.isConnected) previous.focus();
    };
  }, []);

  return (
    <div
      className={`overlay${layer === "popup" ? " popup" : ""}`}
      onMouseDown={(e) => {
        if (closeOnBackdrop && e.target === e.currentTarget) onClose?.();
      }}
    >
      <div ref={boxRef} className={`modal ${className}`.trim()} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1}>
        {header(titleId)}
        {children}
      </div>
    </div>
  );
}
