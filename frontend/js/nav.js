import { getSession, isAdmin, signOut } from "./cognito.js";

// The nav is driven by the session's actual role, not by which page asked
// for it - the admin account only ever sees the admin nav, even on the
// public menu page, since it browses the menu read-only rather than as a
// customer. Customer-facing links (My Orders/My Account) never appear for it.
export function renderNav(targetId = "nav") {
  const el = document.getElementById(targetId);
  if (!el) return;

  const session = getSession();
  const admin = isAdmin(session);
  const brand = `<a class="brand" href="/index.html"><img src="/assets/logo.png" alt="GTX Meals" /><span>GTX MEALS</span></a>`;
  const links = [`<a href="/index.html">Menu</a>`];

  if (admin) {
    // Admin section has its own nav - no customer-facing "My Orders"/"My Account".
    links.push(`<a href="/admin.html">Builder</a>`);
    links.push(`<a href="/admin-orders.html">Orders</a>`);
    links.push(`<a href="/admin-prep.html">Prep List</a>`);
    links.push(`<a href="/customers.html">Customers</a>`);
    links.push(`<a href="#" id="nav-logout">Log out</a>`);
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
