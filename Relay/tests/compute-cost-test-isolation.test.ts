import { expect, it } from 'vitest';

it('does not inherit the resident ComputeCost capture sink in unit tests', () => {
  expect(process.env['PULSE_COMPUTECOST_RECEIPT_DIR']).toBe('');
});
