import { pickupLocationText, paymentLabel, amountPaidCents, balanceCents } from "/js/pricing.js";

// "Export to Excel" on the Orders page: the orders on screen as a CSV file
// Excel and Google Sheets open directly. Laid out like the old Google Form
// responses sheet - one row per order, then for each meal on the orders its
// quantity and customizations side by side, and a Totals row at the bottom.

// Cells Excel would read as a formula (=, +, -, @) are prefixed with ' so a
// customer's note can't run as one.
function cell(value) {
  let text = value == null ? "" : String(value);
  // Plain numbers (a refund's "-5.00") stay numbers.
  if (typeof value !== "number" && !/^-?\d+(\.\d+)?$/.test(text) && /^[=+\-@]/.test(text)) text = `'${text}`;
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

const dollars = (cents) => (cents / 100).toFixed(2);

// "2 Regular, 1 Large with Cauliflower Rice" - this order's lines of one meal.
function customizations(lines) {
  const counts = new Map();
  for (const line of lines) {
    const choice = (line.selectedAddOns || []).map((a) => a.name).join(" + ") || "Regular";
    counts.set(choice, (counts.get(choice) || 0) + line.quantity);
  }
  return [...counts].map(([choice, quantity]) => `${quantity} ${choice}`).join(", ");
}

// orders: as listed by /admin/orders. emailsBySub: customerSub -> email
// (from /customers; a missing one is left blank).
export function ordersToCsv(orders, emailsBySub = new Map()) {
  const mealNames = [...new Set(orders.flatMap((o) => o.items.map((i) => i.name)))].sort((a, b) =>
    a.localeCompare(b)
  );
  const header = [
    "Placed", "Customer", "Email", "Phone", "Pickup Location", "Picked up by", "Ready date", "Status",
    ...mealNames.flatMap((name) => [name, `${name} Customizations`]),
    "Extras & Sauces", "Special Requests", "Meals", "Total ($)", "Payment", "Amount Paid ($)", "Balance ($)", "Meal Suggestions",
  ];

  const mealTotals = new Map(mealNames.map((name) => [name, 0]));
  let mealCount = 0;
  let totalCents = 0;
  let paidCents = 0;
  let balanceTotalCents = 0;
  const rows = orders.map((order) => {
    const counted = order.status !== "cancelled";
    const perMeal = mealNames.flatMap((name) => {
      const lines = order.items.filter((i) => i.name === name);
      const quantity = lines.reduce((sum, i) => sum + i.quantity, 0);
      if (counted) mealTotals.set(name, mealTotals.get(name) + quantity);
      return [quantity, quantity ? customizations(lines) : ""];
    });
    const meals = order.items.reduce((sum, i) => sum + i.quantity, 0);
    if (counted) {
      mealCount += meals;
      totalCents += order.totalCents;
    }
    // Money is totalled for every order shown - a cancelled order's refund
    // still counts toward the balance.
    paidCents += amountPaidCents(order);
    balanceTotalCents += balanceCents(order);
    return [
      new Date(order.createdAt).toLocaleString(),
      order.customerName,
      emailsBySub.get(order.customerSub) || "",
      order.contactPhone,
      pickupLocationText(order.pickupLocation),
      order.pickupName || "Customer",
      order.readyDate,
      order.status,
      ...perMeal,
      (order.extras || []).map((e) => `${e.quantity} x ${e.name}`).join(", "),
      order.items.filter((i) => i.note).map((i) => `${i.name}: ${i.note}`).join("; "),
      meals,
      dollars(order.totalCents),
      paymentLabel(order.payment),
      dollars(amountPaidCents(order)),
      dollars(balanceCents(order)),
      order.mealSuggestions || "",
    ];
  });

  // Cancelled orders are listed (when shown) but left out of the totals.
  const totals = [
    "", "Totals", "", "", "", "", "", "",
    ...mealNames.flatMap((name) => [mealTotals.get(name), ""]),
    "", "", mealCount, dollars(totalCents), "", dollars(paidCents), dollars(balanceTotalCents), "",
  ];

  return [header, ...rows, [], totals].map((row) => row.map(cell).join(",")).join("\r\n");
}

export function downloadCsv(filename, csv) {
  // The byte-order mark tells Excel the file is UTF-8, so names with
  // accents come through intact.
  const blob = new Blob(["﻿", csv], { type: "text/csv;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}
