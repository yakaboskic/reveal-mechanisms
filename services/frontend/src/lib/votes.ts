export type Vote = -1 | 0 | 1;
/** Pressing the selected arrow removes that vote; the other arrow changes it. */
export const nextVote = (current: Vote | null, direction: -1 | 1): Vote => current === direction ? 0 : direction;
