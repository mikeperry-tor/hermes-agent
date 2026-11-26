import { hasInterpolation, INTERPOLATION_RE } from '@hermes/shared/interpolation'

export const PASTE_SNIPPET_RE = /\[\[[^\n]*?\]\]/g

// Keep offsets for replacement. Newlines also prevent a shell match from
// starting on one side of a chip and finishing on the other.
const withoutPasteChips = (text: string) => text.replace(PASTE_SNIPPET_RE, chip => '\n'.repeat(chip.length))

const shellTemplate = (display: string, expanded: string) =>
  display.trim() === expanded ? display : withoutPasteChips(display)

export function visibleInterpolations(display: string, labels: readonly string[]): RegExpMatchArray[] {
  const visible = labels.reduce((text, label) => text.replaceAll(label, '\n'.repeat(label.length)), display)

  return [...visible.matchAll(INTERPOLATION_RE)]
}
export const hasVisibleInterpolation = (display: string, expanded = display.trim()) =>
  hasInterpolation(shellTemplate(display, expanded))
export const hasShellSubmission = (display: string, expanded: string) =>
  display.startsWith('!') || hasVisibleInterpolation(display, expanded)
