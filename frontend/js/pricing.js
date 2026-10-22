// Mirrors the server's price computation in backend/lambda_src/index.py for
// live cart/preview display. The server always recomputes authoritatively at
// checkout - this is display-only.

export function computeUnitPriceCents(meal, allAddOns, selectedAddOnIds) {
  let price = meal.priceCents;
  for (const id of selectedAddOnIds) {
    const addOn = allAddOns.find((a) => a.addOnId === id);
    if (addOn) price += addOnDetailFor(meal, addOn).priceCents;
  }
  return price;
}

// This meal's own price, description and macros for an add-on (set per
// meal on Manage menu). Without its own price the add-on's applies. A meal
// saved before the add-on existed has no description or macros for it, so
// it isn't offered there (see isAddOnOffered) - a new add-on like a
// lettuce wrap only shows on the meals it's filled in for.
export function addOnDetailFor(meal, addOn) {
  const detail = (meal.addOnDetails || {})[addOn.addOnId];
  return {
    priceCents: detail && detail.priceCents != null ? detail.priceCents : addOn.priceCents,
    description: detail ? detail.description : "",
    macros: detail ? detail.macros : null,
  };
}

// An add-on is only offered on a meal that has something to say about it -
// a description or macros. Leaving both blank on Manage menu is how an
// add-on is left off a meal (e.g. no cauliflower rice for the tacos).
export function isAddOnOffered(meal, addOn) {
  const { description, macros } = addOnDetailFor(meal, addOn);
  return !!description || Object.values(macros || {}).some(Boolean);
}

export function describeAddOns(allAddOns, selectedAddOnIds) {
  return selectedAddOnIds
    .map((id) => allAddOns.find((a) => a.addOnId === id))
    .filter(Boolean)
    .map((a) => a.name);
}

// Same compact macro summary everywhere one's shown - meals and add-ons
// both use this shape ({calories, proteinG, carbsG, fatG}).
export function formatMacros(macros) {
  if (!macros) return "";
  const parts = [];
  if (macros.calories) parts.push(`${macros.calories} cal`);
  if (macros.proteinG) parts.push(`${macros.proteinG}g protein`);
  if (macros.carbsG) parts.push(`${macros.carbsG}g carbs`);
  if (macros.fatG) parts.push(`${macros.fatG}g fat`);
  return parts.join(" &middot; ");
}

// Payment as the admin records it on the Orders page (checkout takes no
// payment). Same values as PAYMENT_STATUSES in the API.
export const PAYMENT_LABELS = { unpaid: "Unpaid", venmo: "Paid - Venmo", cash: "Paid - Cash", free: "Free" };

export function paymentLabel(payment) {
  return PAYMENT_LABELS[payment] || PAYMENT_LABELS.unpaid;
}

// Where a customer picks up when the admin hasn't set a location for them
// on the Customers page (shown at checkout and in the Excel export).
export const DEFAULT_PICKUP_LOCATION = "The Gym TX, Georgetown";

export function pickupLocationText(location) {
  return (location || "").trim() || DEFAULT_PICKUP_LOCATION;
}

// (555) 555-5555 as the user types - formats from the raw digits typed so
// far, dropping anything past 10 digits. Shared so every phone input in the
// app (account, checkout) masks the same way.
export function formatPhone(value) {
  const digits = value.replace(/\D/g, "").slice(0, 10);
  if (digits.length > 6) return `(${digits.slice(0, 3)}) ${digits.slice(3, 6)}-${digits.slice(6)}`;
  if (digits.length > 3) return `(${digits.slice(0, 3)}) ${digits.slice(3)}`;
  if (digits.length > 0) return `(${digits}`;
  return "";
}

// A stable string key so "Bowl + Large" and "Bowl (no add-ons)" are distinct
// cart lines, but re-selecting the same add-ons (and note) collapses onto
// one line. Note is part of the key too, so "Bowl, hold the cheese" and a
// plain "Bowl" stay separate instead of one overwriting the other's note.
export function selectionsKey(mealId, selectedAddOnIds, note = "") {
  return `${mealId}|${[...selectedAddOnIds].sort().join(",")}|${note}`;
}
