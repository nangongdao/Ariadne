/**
 * 骨架屏 —— 数据加载时的占位，替代纯文字"加载中"。
 *
 * 形状模拟真实布局（表头 + 行 / 卡片网格），避免加载完成后内容跳变。
 * 流光底由 styles/feedback.css 的 .skeleton 提供。
 */

interface TableSkeletonProps {
  /** 含表头在内的列数 */
  cols?: number;
  rows?: number;
}

export function TableSkeleton({ cols = 6, rows = 8 }: TableSkeletonProps) {
  return (
    <div className="table-skeleton" aria-hidden role="presentation">
      <div className="table-skeleton-head">
        {Array.from({ length: cols }, (_, i) => (
          <div key={i} className="skeleton" style={{ width: `${56 + ((i * 37) % 40)}%` }} />
        ))}
      </div>
      {Array.from({ length: rows }, (_, r) => (
        <div
          key={r}
          className="table-skeleton-row"
          style={{ animationDelay: `${r * 45}ms` }}
        >
          {Array.from({ length: cols }, (_, i) => (
            <div
              key={i}
              className="skeleton"
              style={{
                width: `${40 + ((i * 53 + r * 29) % 55)}%`,
                animationDelay: `${(i * 90 + r * 30) % 700}ms`,
              }}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

export function CardGridSkeleton({ count = 3 }: { count?: number }) {
  return (
    <div className="card-skeleton-grid" aria-hidden role="presentation">
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="card-skeleton" style={{ animationDelay: `${i * 70}ms` }}>
          <div className="skeleton" style={{ height: 16, width: "55%" }} />
          <div className="skeleton" style={{ height: 12, width: "80%" }} />
          <div className="skeleton" style={{ height: 12, width: "70%" }} />
          <div className="skeleton" style={{ height: 26, width: "40%" }} />
        </div>
      ))}
    </div>
  );
}
