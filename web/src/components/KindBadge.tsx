import type { SpanKind } from "@/api/types";
import { kindLabel } from "@/lib/format";

interface Props {
  kind: SpanKind;
}

export function KindBadge({ kind }: Props) {
  return (
    <span className={`kind-badge kind-${kind}`} title={`类型：${kindLabel(kind)}`}>
      {kindLabel(kind)}
    </span>
  );
}
