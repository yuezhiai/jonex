export type VisualMatch = 'MATCH' | 'NO_MATCH' | 'UNCERTAIN';

/** Do not treat a missing image receipt or an ambiguous model reply as a match. */
export function visualMatch(delivered: boolean, observation: string): VisualMatch {
  const marker = observation.match(/^\s*(MATCH|NO_MATCH|UNCERTAIN)\b/i)?.[1]?.toUpperCase();
  return delivered && (marker === 'MATCH' || marker === 'NO_MATCH') ? marker : 'UNCERTAIN';
}
