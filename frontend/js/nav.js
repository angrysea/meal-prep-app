import { getSession, signOut } from "./cognito.js";

export function renderNav(targetId = "nav", { adminNav = false } = {}) {
  const el = document.getElementById(targetId);
  if (!el) return;

  const session = getSession();
  const brand = `<a class="brand" href="/index.html"><img src="/assets/logo.png" alt="GTX Meals" /><span>GTX MEALS</span></a>`;
  const links = [`<a href="/index.html">Menu</a>`];

  if (adminNav) {
    // Admin section has its own nav - no customer-facing "My Orders"/"My Account".
    links.push(`<a href="/admin.html">Admin</a>`);
    links.push(`<a href="/admin-orders.html">Orders</a>`);
    links.push(`<a href="/customers.html">Customers</a>`);
    if (session) links.push(`<a href="#" id="nav-logout">Log out</a>`);
  } else if (session) {
    links.push(`<a href="/orders.html">My Orders</a>`);
    links.push(`<a href="/account.html">My Account</a>`);
    links.push(`<a href="#" id="nav-logout">Log out</a>`);
  } else {
    links.push(`<a href="/login.html">Log in / Sign up</a>`);
  }

  el.innerHTML = `<nav>${brand}${links.join("")}</nav>`;

  const logout = document.getElementById("nav-logout");
  if (logout) {
    logout.addEventListener("click", (e) => {
      e.preventDefault();
      signOut();
      window.location.href = "/index.html";
    });
  }
}
