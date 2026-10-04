let cached = null;

export async function loadConfig() {
  if (cached) return cached;
  const res = await fetch("/config.json");
  if (!res.ok) throw new Error("failed to load config.json");
  cached = await res.json();
  return cached;
}
