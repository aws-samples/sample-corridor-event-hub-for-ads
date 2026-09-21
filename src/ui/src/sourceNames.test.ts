/**
 * Source-naming tests. The bug behind them: the strip and every table named the
 * traffic feed "aws-location-traffic", a string that is an internal id and not the
 * name of any Amazon product. The two things worth pinning are that the mapped
 * name is the real product name, and that an UNMAPPED id comes back untouched -
 * a prettifying fallback would have invented plausible-looking names for feeds
 * nobody had checked.
 */

import { describe, it, expect } from 'vitest';
import { sourceName, sourceNameWithId, sourceShortName } from './sourceNames';

describe('sourceName', () => {
  it('names the traffic feed after the service, not the source id', () => {
    expect(sourceName('aws-location-traffic')).toBe('Amazon Location Service traffic');
  });

  it('names every catalog feed in the strip export', () => {
    // The ids the exporter actually emits today. A new feed failing this is the
    // signal to add it to the map, not to loosen the test.
    for (const id of [
      'ok-odot-wzdx',
      'tx-dot-wzdx',
      'az511-events',
      'nws-alerts',
      'nm-dot-weathershare',
      'aws-location-traffic',
    ]) {
      expect(sourceName(id)).not.toBe(id);
    }
  });

  it('returns an unknown id unchanged rather than guessing', () => {
    expect(sourceName('some-new-feed')).toBe('some-new-feed');
    expect(sourceShortName('some-new-feed')).toBe('some-new-feed');
    expect(sourceNameWithId('some-new-feed')).toBe('some-new-feed');
  });
});

describe('sourceShortName', () => {
  it('stays inside the strip gutter', () => {
    // PAD_L is 152 with a 10px gap; at the 11px label size ~24 characters fit.
    // A longer name does not wrap, it clips - so the budget is a test, not a note.
    for (const id of [
      'ok-odot-wzdx',
      'tx-dot-wzdx',
      'az511-events',
      'nws-alerts',
      'nm-dot-weathershare',
      'aws-location-traffic',
    ]) {
      expect(sourceShortName(id).length).toBeLessThanOrEqual(24);
    }
  });
});

describe('sourceNameWithId', () => {
  it('keeps the id reachable for anything showing a name instead', () => {
    expect(sourceNameWithId('aws-location-traffic')).toBe(
      'Amazon Location Service traffic (aws-location-traffic)',
    );
  });
});
