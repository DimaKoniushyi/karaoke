export const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
export const clamp01 = (value) => clamp(value, 0, 1);
export const clampWithFallback = (value, min, max, fallback = min) => {
  const number = Number(value);
  return Number.isFinite(number) ? clamp(number, min, max) : fallback;
};
export const formatPercent = (value) => `${Math.round(value * 100)}%`;
