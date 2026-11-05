/**
 * 递增请求序号守卫：快速切换 run / 筛选时，只有最新一次请求的响应可以落地。
 *
 * 用法：
 * ```ts
 * const guard = useRef(createSequenceGuard()).current;
 * const seq = guard.begin();
 * const res = await fetch();
 * if (!guard.isLatest(seq)) return;   // 旧响应丢弃
 * ```
 */
export interface SequenceGuard {
  /** 开始一次新请求，返回本次序号（单调递增）。 */
  begin(): number;
  /** 本次响应是否仍是最新请求；false 表示已有更新的请求发出。 */
  isLatest(seq: number): boolean;
}

export function createSequenceGuard(): SequenceGuard {
  let latest = 0;
  return {
    begin() {
      latest += 1;
      return latest;
    },
    isLatest(seq: number) {
      return seq === latest;
    },
  };
}
