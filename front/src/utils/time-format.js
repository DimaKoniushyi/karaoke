export function formatClockTime(seconds, { padMinutes = false } = {}) {
  const numeric = Number(seconds);
  const safe = Number.isFinite(numeric) ? Math.max(0, numeric) : 0;
  const minutes = String(Math.floor(safe / 60));
  return `${padMinutes ? minutes.padStart(2, "0") : minutes}:${String(Math.floor(safe % 60)).padStart(2, "0")}`;
}

// A date/time value from the backend or user input that may be missing or
// unparsable -- toLocale receives a real Date only once value is confirmed
// truthy and parses cleanly, otherwise "—" is returned without calling it.
export function formatSafeDate(value, toLocale) {
  const date = new Date(value);
  return value && !Number.isNaN(+date) ? toLocale(date) : "—";
}
