// Mirrors the server's price computation in backend/lambda_src/index.py for
// live cart/preview display. The server always recomputes authoritatively at
// checkout - this is display-only.

export function findOption(meal, groupId, optionId) {
  const group = (meal.optionGroups || []).find((g) => g.id === groupId);
  return group && group.options.find((o) => o.id === optionId);
}

export function computeUnitPriceCents(meal, selectedOptions) {
  let price = meal.priceCents;
  for (const sel of selectedOptions) {
    const option = findOption(meal, sel.groupId, sel.optionId);
    if (option) price += option.priceDeltaCents;
  }
  return price;
}

export function describeSelections(meal, selectedOptions) {
  return selectedOptions
    .map((sel) => findOption(meal, sel.groupId, sel.optionId))
    .filter(Boolean)
    .map((o) => o.label);
}

// A stable string key so "Bowl + Large" and "Bowl + Regular" are distinct
// cart lines, but re-selecting the same options collapses onto one line.
export function selectionsKey(mealId, selectedOptions) {
  const sorted = [...selectedOptions].sort((a, b) =>
    `${a.groupId}:${a.optionId}`.localeCompare(`${b.groupId}:${b.optionId}`)
  );
  return `${mealId}|${sorted.map((s) => `${s.groupId}=${s.optionId}`).join(",")}`;
}
