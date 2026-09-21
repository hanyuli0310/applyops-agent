import { stateClass, stateLabel } from "../dashboard";

export function StatusBadge({ state, label }: { state: string; label?: string }) {
  return <span className={`status-badge ${stateClass(state)}`}>{label ?? stateLabel(state)}</span>;
}

