export function withBase(path = ""): string {
  const base = import.meta.env.BASE_URL;
  const normalized = path.replace(/^\/+/, "");
  return normalized ? `${base}${normalized}` : base;
}

export function formatIssueDate(value: Date): string {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone: "Asia/Shanghai",
  })
    .format(value)
    .replaceAll("/", ".");
}

export function isoDate(value: Date): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone: "Asia/Shanghai",
  }).formatToParts(value);
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")}`;
}
