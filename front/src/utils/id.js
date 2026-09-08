export const generateId = () =>
  globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;

export const sameId = (a, b) => a != null && b != null && String(a) === String(b);
