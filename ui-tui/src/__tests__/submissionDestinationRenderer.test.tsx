import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PassThrough } from 'node:stream'

import { renderSync, Text } from '@hermes/ink'
import React from 'react'
import { expect, it, vi } from 'vitest'

import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import { captureDestination } from '../app/submissionDestination.js'
import { useSubmission } from '../app/useSubmission.js'
import { useQueue } from '../hooks/useQueue.js'
import { loadPendingInputs } from '../lib/pendingInputs.js'
import { pasteTokenLabel } from '../lib/text.js'
import { visibleInterpolations } from '../protocol/paste.js'

it('finds typed shell syntax without interpreting an entire registered paste chip', () => {
  const chip = '[[ a b c d ] ]] {!printf chip} [5 lines] ]]'
  const visible = `${chip} [[ {!printf typed} ]]`

  expect(visibleInterpolations(visible, [chip]).map(match => match[1])).toEqual(['printf typed'])
})

it('submits interpolated queued input only to its captured owner and removes it only after the matching receipt', async () => {
  const home = mkdtempSync(join(tmpdir(), 'ink-submit-'))
  vi.stubEnv('HERMES_HOME', home)
  resetUiState()
  const info = { model: 'test', profile_name: 'alpha', stored_session_id: 'stored-owner', skills: {}, tools: {} }
  patchUiState({ sid: 'owner', info })
  let resolveShell!: (value: unknown) => void
  let resolveSubmit!: (value: unknown) => void

  const request = vi.fn((method: string) => {
    if (method === 'shell.exec') {
      return new Promise(resolve => {
        resolveShell = resolve
      })
    }

    if (method === 'input.detect_drop') {
      return Promise.resolve({ matched: false })
    }

    return new Promise(resolve => {
      resolveSubmit = resolve
    })
  })

  let queue!: ReturnType<typeof useQueue>
  let submission!: ReturnType<typeof useSubmission>
  const appended = vi.fn()

  function Harness() {
    queue = useQueue()
    submission = useSubmission({
      appendMessage: appended,
      composerActions: { ...queue, prependQueue: queue.prependQ, takeQueue: queue.takeQ } as any,
      composerRefs: { queueRef: queue.queueRef, queueEditRef: queue.queueEditRef, tokensRef: { current: [] } } as any,
      composerState: { input: '', inputBuf: [], completions: [] } as any,
      gw: { request } as any,
      setLastUserMsg: vi.fn(),
      slashRef: { current: () => true },
      submitRef: { current: () => {} },
      sys: vi.fn()
    })

    return <Text>{queue.queuedDisplay.join('|')}</Text>
  }

  const stdout = Object.assign(new PassThrough(), { columns: 80, rows: 20, isTTY: false })

  const instance = renderSync(<Harness />, {
    stdin: new PassThrough() as any,
    stdout: stdout as any,
    stderr: new PassThrough() as any,
    patchConsole: false
  })

  try {
    queue.enqueue('private {!printf data}')
    const item = queue.dequeue()!
    patchUiState({ sid: 'other', busy: false })
    submission.sendQueued(item)
    expect(getUiState().busy).toBe(false)
    expect(appended).not.toHaveBeenCalled()
    resolveShell({ stdout: 'data', code: 0 })
    await expect.poll(() => request.mock.calls.filter(call => call[0] === 'prompt.submit').length).toBe(1)
    expect(request).toHaveBeenCalledWith('prompt.submit', {
      session_id: 'owner',
      text: 'private data',
      submission_id: item.submissionId,
      queued: true
    })
    expect(appended).not.toHaveBeenCalled()
    expect(queue.queueRef.current).toEqual([])
    patchUiState({ sid: 'owner' })
    expect(queue.queueRef.current).toContain(item)
    resolveSubmit({
      admission_id: item.submissionId,
      target_session_id: 'stored-owner',
      target_profile_home: home,
      status: 'queued'
    })
    await expect.poll(() => queue.queueRef.current.length).toBe(0)
  } finally {
    instance.unmount()
    resetUiState()
    vi.unstubAllEnvs()
    rmSync(home, { recursive: true, force: true })
  }
})

it.each(['refused', 'cancelled', 'busy', 'paste', 'edit'])('routes queued shell input for %s', async outcome => {
  const home = mkdtempSync(join(tmpdir(), 'ink-submit-'))
  vi.stubEnv('HERMES_HOME', home)
  resetUiState()
  patchUiState({ sid: 'owner', info: { model: 'test', stored_session_id: 'stored-owner', skills: {}, tools: {} } as any })

  let completeShell!: (result: unknown) => void
  const request = vi.fn((method: string) => {
    if (method !== 'shell.exec') {
      return Promise.resolve({})
    }

    return outcome === 'refused'
      ? Promise.reject(Object.assign(new Error('dangerous_command'), { code: 4001 }))
      : new Promise(resolve => { completeShell = resolve })
  })

  let queue!: ReturnType<typeof useQueue>
  let submission!: ReturnType<typeof useSubmission>
  const sys = vi.fn()

  function Harness() {
    queue = useQueue()
    submission = useSubmission({
      appendMessage: vi.fn(),
      composerActions: {
        ...queue,
        clearIn: vi.fn(),
        prependQueue: queue.prependQ,
        pushHistory: vi.fn(),
        takeQueue: queue.takeQ
      } as any,
      composerRefs: { queueRef: queue.queueRef, queueEditRef: queue.queueEditRef, tokensRef: { current: [] } } as any,
      composerState: { input: '', inputBuf: [], completions: [] } as any,
      gw: { request, isCanonical: true } as any,
      setLastUserMsg: vi.fn(),
      slashRef: { current: () => true },
      submitRef: { current: () => {} },
      sys
    })

    return <Text>{queue.queuedDisplay.join('|')}</Text>
  }

  const stdout = Object.assign(new PassThrough(), { columns: 80, rows: 20, isTTY: false })

  const instance = renderSync(<Harness />, {
    stdin: new PassThrough() as any,
    stdout: stdout as any,
    stderr: new PassThrough() as any,
    patchConsole: false
  })

  try {
    const text = 'summarize {!printf first} and {!printf second}'

    if (outcome === 'edit') {
      queue.enqueue('!printf old')
      queue.setQueueEdit(0)
      submission.dispatchSubmission('!printf new')
      patchUiState({ sid: 'other', busy: false })
      await expect.poll(() => request.mock.calls.length).toBe(1)
      expect(request).toHaveBeenCalledWith('shell.exec', {
        command: 'printf new',
        session_id: 'owner'
      })
      expect(getUiState().busy).toBe(false)
      completeShell({ stdout: 'new', code: 0 })
      patchUiState({ sid: 'owner' })
      await expect.poll(() => loadPendingInputs(captureDestination()).length).toBe(0)
      expect(request.mock.calls.filter(([method]) => method === 'shell.exec')).toHaveLength(1)
      return
    } else if (outcome === 'busy') {
      patchUiState({ busy: true, busyInputMode: 'steer' })
      submission.dispatchSubmission(text)
      expect(request).not.toHaveBeenCalled()
      patchUiState({ busy: false })
    } else if (outcome === 'paste') {
      const body = 'a\nb\nc\nd\n]]] {!id}'
      const chip = pasteTokenLabel(body, 5)
      expect(chip).toContain('{!id}')
      queue.enqueue(`summarize ${body}`, `summarize ${chip}`)
    } else {
      queue.enqueue(text)
    }

    if (outcome !== 'paste') {
      const [restored] = loadPendingInputs(captureDestination())
      expect(restored?.failed).toBe(true)
    }

    const item = queue.dequeue()!
    submission.sendQueued(item)

    if (outcome === 'paste') {
      await expect.poll(() => request.mock.calls.some(([method]) => method === 'prompt.submit')).toBe(true)
      expect(request.mock.calls.some(([method]) => method === 'shell.exec')).toBe(false)
      return
    }

    if (outcome !== 'refused') {
      expect(submission.cancelShell()).toBe(true)
      expect(request).toHaveBeenCalledWith('shell.cancel', { session_id: 'owner' })
      completeShell({ stdout: 'first', code: 0 })
    }

    await expect.poll(() => sys.mock.calls.length).toBe(1)
    const methods = request.mock.calls.map(([method]) => method)
    expect(methods).not.toContain('prompt.submit')
    expect(methods.filter(method => method === 'shell.exec')).toHaveLength(1)
    expect(String(sys.mock.calls[0]![0])).toContain(outcome === 'refused' ? 'dangerous_command' : 'command_interrupted')
    // Retained for Alt+K / an edit, never silently dropped.
    expect(item.failed).toBe(true)
    expect(queue.queueRef.current).toContain(item)
  } finally {
    instance.unmount()
    resetUiState()
    vi.unstubAllEnvs()
    rmSync(home, { recursive: true, force: true })
  }
})
