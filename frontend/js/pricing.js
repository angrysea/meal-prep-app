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

// A stable string key so "Bowl + Large" and "Bowl (no add-ons)" are distinct
// cart lines, but re-selecting the same add-ons (and note) collapses onto
// one line. Note is part of the key too, so "Bowl, hold the cheese" and a
// plain "Bowl" stay separate instead of one overwriting the other's note.
export function selectionsKey(mealId, selectedAddOnIds, note = "") {
  return `${mealId}|${[...selectedAddOnIds].sort().join(",")}|${note}`;
}
