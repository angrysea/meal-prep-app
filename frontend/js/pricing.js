// Mirrors the server's price computation in backend/lambda_src/index.py for
// live cart/preview display. The server always recomputes authoritatively at
// checkout - this is display-only.

export function computeUnitPriceCents(meal, allAddOns, selectedAddOnIds) {
  let price = meal.priceCents;
  for (const id of selectedAddOnIds) {
    const addOn = allAddOns.find((a) => a.addOnId === id);
    if (addOn) price += addOn.priceCents;
  }
  return price;
}

export function describeAddOns(allAddOns, selectedAddOnIds) {
  return selectedAddOnIds
    .map((id) => allAddOns.find((a) => a.addOnId === id))
    .filter(Boolean)
    .map((a) => a.description);
}

// A stable string key so "Bowl + Large" and "Bowl (no add-ons)" are distinct
// cart lines, but re-selecting the same add-ons collapses onto one line.
export function selectionsKey(mealId, selectedAddOnIds) {
  return `${mealId}|${[...selectedAddOnIds].sort().join(",")}`;
}
