// meiva:link ui/web/src/docs/frontend-components/inspector/inspector.md
export const INSPECT_SECTION_ORDER = {
  costActivity: 900,
  context: 600,
  runLink: 100,
  page: 200,
  shells: 300,
  schedules: 350,
  liveness: 400,
  configOverlay: 500,
} as const;

export function fleetTaskHref(id: number): `/fleet?task=${number}` {
  return `/fleet?task=${id}`;
}

export function scheduleHref(id: number): `/control/schedules?schedule=${number}` {
  return `/control/schedules?schedule=${id}`;
}
