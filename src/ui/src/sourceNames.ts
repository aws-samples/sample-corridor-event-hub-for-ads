/**
 * Human-facing names for the feeds.
 *
 * `sourceId` is an IDENTIFIER, not a product name. Printing it raw was mis-naming
 * the vendor in the one place a reader would take it as authoritative: the traffic
 * feed is Amazon Location Service, and there is no product called
 * "aws-location-traffic". The ids themselves stay as they are - they are the join
 * key across the catalog, the adapters, the fixtures and the stacks, and renaming
 * one to fix a caption would be the wrong trade.
 *
 * Two forms because the strip gutter is 130-odd pixels wide and the tables are not.
 * `short` is what fits beside a row; `name` is what a sentence or a table cell says.
 *
 * An id with no entry here falls back to itself. That is deliberate: a feed added
 * to the catalog and not to this map renders as a visible raw id rather than a
 * plausible-looking guess, which is the failure that gets noticed and fixed.
 */
interface SourceName {
  name: string;
  short: string;
}

const NAMES: Record<string, SourceName> = {
  'ok-odot-wzdx': { name: 'Oklahoma DOT WZDx', short: 'Oklahoma DOT WZDx' },
  'tx-dot-wzdx': { name: 'Texas DOT WZDx', short: 'Texas DOT WZDx' },
  'az511-events': { name: 'AZ511 events', short: 'AZ511 events' },
  'nws-alerts': { name: 'NWS alerts', short: 'NWS alerts' },
  'nm-dot-weathershare': {
    name: 'New Mexico DOT WeatherShare',
    short: 'NMDOT WeatherShare',
  },
  'aws-location-traffic': {
    name: 'Amazon Location Service traffic',
    short: 'Amazon Location traffic',
  },
};

/** Full display name, for prose and table cells. Unknown ids return unchanged. */
export function sourceName(sourceId: string): string {
  return NAMES[sourceId]?.name ?? sourceId;
}

/** Gutter-width display name. Unknown ids return unchanged. */
export function sourceShortName(sourceId: string): string {
  return NAMES[sourceId]?.short ?? sourceId;
}

/** `Name (id)`, for the tooltip on anything that shows a name instead of the id. */
export function sourceNameWithId(sourceId: string): string {
  const named = NAMES[sourceId];
  return named ? `${named.name} (${sourceId})` : sourceId;
}
