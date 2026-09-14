// Deliberately excludes "button"/"a[href]": focus merely resting on a
// control after a click must not swallow global hotkeys (Escape/seek), and
// none of these hotkeys trigger a focused button/link's own native key
// activation (that's Enter/Space, which the karaoke Space handling and
// preventDefault() already guard against), so there is nothing to protect
// there.
const EDITABLE = [
  "input",
  "select",
  "textarea",
  '[contenteditable=""]',
  '[contenteditable="true"]',
  '[role="textbox"]',
  '[role="slider"]',
  '[data-hotkeys="off"]'
].join(", ");
export const isEditableHotkeyTarget = (target) => Boolean(target?.closest?.(EDITABLE));
export function isHotkeyScopeActive(scope) {
  if (!scope?.isConnected) return false;
  const dialogs = globalThis.document?.querySelectorAll?.('[role="dialog"][aria-modal="true"]');
  const top = dialogs ? [...dialogs].at(-1) : null;
  return !top || top.contains(scope);
}
export const shouldIgnoreHotkey = (event, scope) =>
  !event ||
  event.defaultPrevented ||
  event.isComposing ||
  event.repeat ||
  isEditableHotkeyTarget(event.target) ||
  !isHotkeyScopeActive(scope);
