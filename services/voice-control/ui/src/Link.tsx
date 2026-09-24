/* In-app links over the History API; see router.ts. */
import type { AnchorHTMLAttributes, MouseEvent } from 'react';
import { navigate } from './router';

/* An ordinary anchor that navigates in-app on a plain left click, and behaves
 * as a normal link for modified clicks (new tab, new window). */
export function Link({
  href,
  onClick,
  ...rest
}: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) {
  const handle = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event);
    if (event.defaultPrevented) return;
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    navigate(href);
  };
  return <a href={href} onClick={handle} {...rest} />;
}
