export const noop = () => {};

// Runs a task on the next microtask and swallows any rejection, resolving to
// `fallback` instead -- for fire-and-forget work (media playback, room sync,
// cleanup) where a failure must not surface as an unhandled rejection.
export const safe = (task, fallback) => Promise.resolve().then(task).catch(() => fallback);
