import { useEffect, useState } from 'react';

/**
 * P0-6.3: useMediaQuery——响应式媒体查询 hook
 *
 * 与 Tailwind 断点同源（tailwind.config.ts 默认值）：
 *   sm=640, md=768, lg=1024, xl=1280, 2xl=1536
 *
 * 用法：
 *   const isMobile = useIsMobile();          // < 768px (md 以下)
 *   const isDesktop = useIsDesktop();        // >= 1024px (lg 以上)
 *   const isLarge = useMediaQuery('(min-width: 1280px)');  // 自定义
 *
 * 对标 react-responsive / @react-hook/media-query 的极简实现。
 * 替代手写 `if (window.innerWidth < 768)` 判断——单一真源 + 实时响应。
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => {
    if (typeof window === 'undefined') return false;
    return window.matchMedia(query).matches;
  });

  useEffect(() => {
    if (typeof window === 'undefined') return;
    const mql = window.matchMedia(query);
    const handler = (e: MediaQueryListEvent) => setMatches(e.matches);
    // 初始化时同步一次（防 SSR 不一致）
    setMatches(mql.matches);
    mql.addEventListener('change', handler);
    return () => mql.removeEventListener('change', handler);
  }, [query]);

  return matches;
}

/** 移动端：< md (768px) */
export function useIsMobile(): boolean {
  return useMediaQuery('(max-width: 767px)');
}

/** 桌面端：>= lg (1024px) */
export function useIsDesktop(): boolean {
  return useMediaQuery('(min-width: 1024px)');
}

/** 平板/小桌面：>= md (768px) */
export function useIsTablet(): boolean {
  return useMediaQuery('(min-width: 768px)');
}
