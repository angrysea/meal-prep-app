import { getSession, isAdmin, signOut } from "./cognito.js";

export function renderNav(targetId = "nav") {
  const el = document.getElementById(targetId);
  if (!el) return;

  const session = getSession();
  const links = [`<a href="/index.html">Menu</a>`];

  if (session) {
    links.push(`<a href="/orders.html">My Orders</a>`);
    if (isAdmin(session)) links.push(`<a href="/admin.html">Admin</a>`);
    links.push(`<a href="#" id="nav-logout">Log out</a>`);
  } else {
    links.push(`<a href="/login.html">Log in / Sign up</a>`);
  }

  el.innerHTML = `<nav>${links.join("")}</nav>`;

  const logout = document.getElementById("nav-logout");
  if (logout) {
    logout.addEventListener("click", (e) => {
      e.preventDefault();
      signOut();
      window.location.href = "/index.html";
    });
  }
}
