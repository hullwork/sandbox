import { LoaderCircle, RefreshCw } from "lucide-react";
import { useState } from "react";

/**
 * Refresh control with its own pending state.
 *
 * The views own a `loading` flag that is only true for the first load, so a manual
 * refresh used to give no feedback at all: the button stayed idle and a slow Control Plane
 * looked like a dead click. Holding the pending state here keeps that fix out of
 * every view's state and matches the sites console `RefreshButton`.
 *
 * The label is passed through unchanged: this control adds the spinner, the disabled
 * state and aria-busy, never new copy.
 */
export function RefreshButton({
  onRefresh,
  children,
}: {
  onRefresh: () => void | Promise<unknown>;
  children: React.ReactNode;
}) {
  const [refreshing, setRefreshing] = useState(false);
  const run = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      await onRefresh();
    } finally {
      setRefreshing(false);
    }
  };
  return (
    <button
      type="button"
      className="button button-small"
      disabled={refreshing}
      aria-busy={refreshing}
      onClick={() => void run()}
    >
      {refreshing ? (
        <LoaderCircle className="spin" size={15} aria-hidden="true" />
      ) : (
        <RefreshCw size={15} aria-hidden="true" />
      )}
      {children}
    </button>
  );
}
