import { useEffect, useState } from "react";

export interface RouteState {
  pathname: string;
  search: URLSearchParams;
}

function currentRoute(): RouteState {
  return { pathname: window.location.pathname, search: new URLSearchParams(window.location.search) };
}

export function useRoute(): RouteState {
  const [route, setRoute] = useState(currentRoute);
  useEffect(() => {
    const update = () => setRoute(currentRoute());
    window.addEventListener("popstate", update);
    window.addEventListener("terrarium:navigate", update);
    return () => {
      window.removeEventListener("popstate", update);
      window.removeEventListener("terrarium:navigate", update);
    };
  }, []);
  return route;
}

export function navigate(href: string): void {
  window.history.pushState(null, "", href);
  window.dispatchEvent(new Event("terrarium:navigate"));
  window.scrollTo({ top: 0, behavior: "smooth" });
}

export function Link({ href, className, children, title }: {
  href: string;
  className?: string;
  children: React.ReactNode;
  title?: string;
}): React.JSX.Element {
  return (
    <a
      href={href}
      className={className}
      title={title}
      onClick={(event) => {
        if (
          !event.defaultPrevented &&
          event.button === 0 &&
          !event.metaKey &&
          !event.ctrlKey &&
          !event.shiftKey &&
          !event.altKey
        ) {
          event.preventDefault();
          navigate(href);
        }
      }}
    >
      {children}
    </a>
  );
}
