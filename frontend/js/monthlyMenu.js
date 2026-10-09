// Shared "which meals are on the menu for a given month" logic, used by
// menu-flyer.html for both the weekly and monthly view - one source of
// truth for the membership rule (staple, or weekOf falls in the picked
// month) and the "October 5th" date-heading format.

export const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

// 5 -> "5th", 1 -> "1st", 12 -> "12th", 23 -> "23rd"
export function ordinal(day) {
  const j = day % 10;
  const k = day % 100;
  if (j === 1 && k !== 11) return `${day}st`;
  if (j === 2 && k !== 12) return `${day}nd`;
  if (j === 3 && k !== 13) return `${day}rd`;
  return `${day}th`;
}

export function formatDateHeading(isoDate) {
  const [, month, day] = isoDate.split("-").map(Number);
  return `${MONTH_NAMES[month - 1]} ${ordinal(day)}`;
}

// Staples always show; a rotating meal shows only when its own weekOf falls
// in the picked month ("YYYY-MM") - same membership rule the customer menu
// uses day-of, just evaluated against a whole month here instead of a
// single ready date.
export function groupMealsForMonth(meals, monthValue) {
  const staples = meals.filter((m) => m.available && m.staple);
  const rotating = meals.filter((m) => m.available && !m.staple && m.weekOf && m.weekOf.startsWith(monthValue));

  const byDate = new Map();
  rotating.forEach((m) => {
    if (!byDate.has(m.weekOf)) byDate.set(m.weekOf, []);
    byDate.get(m.weekOf).push(m);
  });
  const sortedDates = [...byDate.keys()].sort();

  return { staples, byDate, sortedDates };
}
