import { expect, it, vi } from 'vitest'

vi.mock('./terminals', () => ({ updateTerminalRestoreCwd: vi.fn() }))

import { parseOscCwd } from './cwd-tracking'

it.each(['file://executor/path%20with%20space%20', 'file://executor/path with space '])('preserves path data in OSC 7: %s', payload => {
  expect(parseOscCwd(7, payload)).toBe('/path with space ')
})

it('preserves whitespace inside quoted OSC 9 paths', () => {
  expect(parseOscCwd(9, '9;"C:\\ leading space\\directory"')).toBe('C:\\ leading space\\directory')
})
