/** components/common/SkipLink.tsx — Task 6.13: skip-to-main-content link
 *
 * WCAG 2.1 AA 要求：在 Tab 顺序第一位提供跳到主内容的链接，
 * 键盘用户可绕过重复导航（侧边栏、标题栏）。
 */
interface SkipLinkProps {
  targetId: string;
  label: string;
}

export function SkipLink({ targetId, label }: SkipLinkProps) {
  return (
    <a
      href={`#${targetId}`}
      className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-skip focus:px-3 focus:py-2 focus:bg-primary focus:text-primary-foreground focus:rounded-md focus:outline-none focus:ring-2 focus:ring-ring"
    >
      {label}
    </a>
  );
}
