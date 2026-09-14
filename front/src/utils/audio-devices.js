import { translateSaved as t } from "../i18n/runtime";

const INPUT_AUXILIARY =
  /sound mapper|primary sound capture|первичн(?:ый|ий).*драйвер.*запис|loop[ -]?back|stereo mix|what u hear|s\/?pdif|adat/i;
const OUTPUT_AUXILIARY =
  /sound mapper|primary sound driver|первичн(?:ый|ий).*звуков.*драйвер|первичн(?:ое|ий).*устройств.*вывод/i;
const HOST_SUFFIX = /\s+\[(?:MME|ASIO|Windows (?:WASAPI|DirectSound|WDM-KS))\]\s*$/i;
const HOSTS = ["wasapi", "asio", "directsound", "mme", "wdm-ks"];
const list = (value) => (Array.isArray(value) ? value.filter(Boolean) : []);
const host = (device) => String(device?.host_api || "").toLowerCase();
const selectedIndex = (value) => {
  const index = Number(value);
  return value != null && value !== "" && Number.isInteger(index) ? index : null;
};
const normalize = (device) => ({
  ...device,
  index: Number(device?.index),
  name: String(device?.name || device?.label || "").trim().replace(HOST_SUFFIX, "").trim()
});
const hostPriority = (device) => {
  const value = host(device);
  const index = HOSTS.findIndex((name) => value.includes(name));
  return index < 0 ? HOSTS.length : index;
};

function addBest(choices, device, selected) {
  const key = device.name.toLocaleLowerCase() || `device-${device.index}`;
  const current = choices.get(key);
  const isSelected = device.index === selected;
  const currentSelected = current?.index === selected;
  if (!current || isSelected || (!currentSelected && hostPriority(device) < hostPriority(current))) {
    choices.set(key, device);
  }
}

function mapDeviceOptions(devices, getValue, fallbackLabel) {
  const seen = new Set();
  return list(devices)
    .map((device) => ({
      value: getValue(device),
      label: device.name || device.label || fallbackLabel
    }))
    .filter(({ value }) => {
      if (value == null || value === "" || seen.has(value)) return false;
      seen.add(value);
      return true;
    });
}

export function createIndexedDeviceOptions(devices, defaultLabel = t("library.sort.relevance")) {
  return [
    { value: "", label: defaultLabel },
    ...mapDeviceOptions(devices, ({ index }) => index, t("karaoke.device"))
  ];
}

export function createInputDeviceOptions(
  devices,
  selectedDeviceId,
  defaultLabel = t("library.sort.relevance")
) {
  const selected = selectedIndex(selectedDeviceId);
  const choices = new Map();

  for (const source of list(devices)) {
    const device = normalize(source);
    if (!Number.isInteger(device.index)) continue;
    const auxiliary = INPUT_AUXILIARY.test(device.name) || host(device).includes("wdm-ks");
    if (auxiliary && device.index !== selected) continue;
    addBest(choices, { ...device, name: device.name || t("karaoke.device") }, selected);
  }

  return createIndexedDeviceOptions([...choices.values()], defaultLabel);
}

export function createOutputDeviceOptions(
  devices,
  selectedDeviceId,
  driver = "auto",
  defaultLabel = t("karaoke.systemDevice")
) {
  const selected = selectedIndex(selectedDeviceId);
  const candidates = list(devices)
    .map(normalize)
    .filter(
      (device) =>
        Number.isInteger(device.index) &&
        Number(device?.max_output_channels ?? 1) > 0 &&
        !OUTPUT_AUXILIARY.test(device.name) &&
        !host(device).includes("wdm-ks")
    );
  const requested = driver === "asio" ? "asio" : driver === "mme" ? "mme" : "wasapi";
  const preferred = [requested, ...HOSTS.filter((name) => name !== requested)].find((name) =>
    candidates.some((device) => host(device).includes(name))
  );
  const choices = new Map();

  for (const device of candidates) {
    const isSelected = device.index === selected && host(device).includes(requested);
    if (preferred && !host(device).includes(preferred) && !isSelected) continue;
    addBest(choices, { ...device, name: device.name || t("karaoke.device") }, selected);
  }

  return createIndexedDeviceOptions([...choices.values()], defaultLabel);
}

export function createBrowserDeviceOptions(
  devices,
  fallbackLabel,
  defaultLabel = t("karaoke.systemDefault")
) {
  return [
    { value: "default", label: defaultLabel },
    ...mapDeviceOptions(devices, ({ deviceId }) => deviceId, fallbackLabel)
  ];
}

export function createBufferSizeOptions(values = [32, 64, 128, 256, 512]) {
  return [...new Set(list(values).map(Number))]
    .filter((value) => Number.isInteger(value) && value > 0)
    .map((value) => ({ value, label: `${value} samples` }));
}
