import {
  forwardRef,
  useImperativeHandle,
  useRef,
  isValidElement,
  cloneElement,
  type CSSProperties,
  type ReactNode,
  type ForwardedRef,
  type ReactElement,
  type Ref,
} from "react";
import { useVirtualizer } from "@tanstack/react-virtual";

export interface VirtualizedMessageListHandle {
  scrollToBottom(behavior?: ScrollBehavior): void;
  /** Task 2.3: 暴露滚动元素以便父组件 attach scroll listener 检测用户上滑 */
  getScrollElement(): HTMLDivElement | null;
}

interface VirtualizedMessageListProps<T extends { id: string }> {
  items: T[];
  renderItem: (item: T, style: CSSProperties) => ReactNode;
  estimateSize?: number;
}

const ITEM_ESTIMATE = 160;

function VirtualizedMessageListImpl<T extends { id: string }>(
  { items, renderItem, estimateSize = ITEM_ESTIMATE }: VirtualizedMessageListProps<T>,
  ref: ForwardedRef<VirtualizedMessageListHandle>,
) {
  const parentRef = useRef<HTMLDivElement>(null);

  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => estimateSize,
    overscan: 5,
    measureElement: (el) => (el as HTMLElement).getBoundingClientRect().height,
  });

  useImperativeHandle(
    ref,
    () => ({
      scrollToBottom: (behavior = "auto") => {
        const last = items.length - 1;
        if (last < 0) return;
        virtualizer.scrollToIndex(last, { align: "end", behavior });
      },
      getScrollElement: () => parentRef.current,
    }),
    [items.length, virtualizer],
  );

  return (
    <div ref={parentRef} className="h-full w-full overflow-y-auto relative" data-testid="virtualized-message-list">
      <div
        style={{
          height: `${virtualizer.getTotalSize()}px`,
          width: "100%",
          position: "relative",
        }}
      >
        {virtualizer.getVirtualItems().map((virtualItem) => {
          const item = items[virtualItem.index];
          const style: CSSProperties = {
            position: "absolute",
            top: 0,
            left: 0,
            width: "100%",
            transform: `translateY(${virtualItem.start}px)`,
          };
          const child = renderItem(item, style);
          if (!isValidElement(child)) return child;
          return cloneElement(
            child as ReactElement<{ style?: CSSProperties }>,
            {
              key: item.id,
              ref: virtualizer.measureElement as unknown as Ref<HTMLElement>,
              "data-index": virtualItem.index,
            } as unknown as { style?: CSSProperties },
          );
        })}
      </div>
    </div>
  );
}

const VirtualizedMessageListInner = forwardRef(VirtualizedMessageListImpl);
VirtualizedMessageListInner.displayName = "VirtualizedMessageList";

export const VirtualizedMessageList = VirtualizedMessageListInner as <T extends { id: string }>(
  props: VirtualizedMessageListProps<T> & { ref?: Ref<VirtualizedMessageListHandle> },
) => ReactElement;
