// Shared "come back here after logging in" helper, used by every page that
// gates on a session and by login.html itself.

export function loginUrlWithReturnTo(path = window.location.pathname) {
  return `/login.html?redirect=${encodeURIComponent(path)}`;
}

export function getSafeRedirectTarget(fallback = "/index.html") {
  const param = new URLSearchParams(window.location.search).get("redirect");
  // Only same-site relative paths - guards against an open redirect via a
  // crafted ?redirect= value (e.g. "//evil.example.com").
  if (param && param.startsWith("/") && !param.startsWith("//")) {
    return param;
  }
  return fallback;
}
