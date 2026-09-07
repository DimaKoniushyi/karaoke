export const getAudioContextClass = () => globalThis.AudioContext || globalThis.webkitAudioContext;

export async function closeAudioContext(context) {
  if (typeof context?.close !== "function" || context.state === "closed") return false;
  try {
    await context.close();
    return true;
  } catch {
    return false;
  }
}
export const closeAudioContextQuietly = (context) => {
  closeAudioContext(context);
};
